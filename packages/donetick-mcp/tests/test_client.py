"""Unit tests for the HTTP client.

Every test runs against an in-process ``httpx.MockTransport``, so these cover
the client's own logic -- auth, retries, error translation, payload shaping --
without needing a Donetick server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from donetick_mcp import (
    ChoreAssignees,
    ChoreReq,
    Config,
    DonetickAuthError,
    DonetickClient,
    DonetickError,
    DonetickNotFoundError,
    DonetickTransportError,
    FrequencyType,
    Label,
    SubTask,
    TokenBucket,
)


def json_response(
    payload: Any, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    async def test_burst_is_available_immediately(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=3)
        for _ in range(3):
            await bucket.acquire()

    async def test_blocks_once_exhausted(self) -> None:
        bucket = TokenBucket(rate=50.0, burst=1)
        await bucket.acquire()
        loop_start = __import__("time").monotonic()
        await bucket.acquire()
        assert __import__("time").monotonic() - loop_start >= 0.01


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    async def test_prefers_access_token_over_legacy_token(self, mock_client: Any) -> None:
        """Current Donetick returns `access_token`; `token` is a legacy mirror."""
        client, rec = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {
                        "access_token": "new-style",
                        "token": "legacy",
                        "access_token_expiry": "2099-01-01T00:00:00Z",
                    }
                )
            }
        )
        await client.ensure_auth(force=True)
        assert client._token == "new-style"
        assert client._client.headers["Authorization"] == "Bearer new-style"
        assert rec.paths() == ["POST /api/v1/auth/login"]
        await client.aclose()

    async def test_falls_back_to_legacy_token_field(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {("POST", "/api/v1/auth/login"): json_response({"token": "old-style"})}
        )
        await client.ensure_auth(force=True)
        assert client._token == "old-style"
        await client.aclose()

    async def test_refresh_token_is_stored_and_reused(self, mock_client: Any) -> None:
        """A valid access token must not trigger another login."""
        client, rec = mock_client({})
        client._refresh_token = "rt-1"
        await client.ensure_auth()
        assert rec.paths() == []
        await client.aclose()

    async def test_uses_refresh_endpoint_before_login(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("POST", "/api/v1/auth/refresh"): json_response(
                    {"access_token": "refreshed", "access_token_expiry": "2099-01-01T00:00:00Z"}
                )
            }
        )
        client._token = ""
        client._token_expire = 0.0
        client._refresh_token = "rt-1"
        await client.ensure_auth()
        assert rec.paths() == ["POST /api/v1/auth/refresh"]
        assert client._token == "refreshed"
        await client.aclose()

    async def test_rejected_refresh_falls_back_to_login(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("POST", "/api/v1/auth/refresh"): json_response(
                    {"error": "invalid refresh token"}, status=401
                ),
                ("POST", "/api/v1/auth/login"): json_response(
                    {"access_token": "from-login", "access_token_expiry": "2099-01-01T00:00:00Z"}
                ),
            }
        )
        client._token = ""
        client._token_expire = 0.0
        client._refresh_token = "stale"
        await client.ensure_auth()
        assert client._token == "from-login"
        assert rec.paths() == [
            "POST /api/v1/auth/refresh",
            "POST /api/v1/auth/login",
        ]
        await client.aclose()

    async def test_mfa_account_raises_a_clear_error(self, mock_client: Any) -> None:
        """MFA logins return no token at all; the old code reported 'unknown error'."""
        client, _ = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {"mfaRequired": True, "sessionToken": "partial"}
                )
            }
        )
        with pytest.raises(DonetickAuthError, match="multi-factor"):
            await client.ensure_auth(force=True)
        await client.aclose()

    async def test_camel_case_mfa_flag_is_detected(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {"mfa_required": True, "sessionToken": "partial"}
                )
            }
        )
        with pytest.raises(DonetickAuthError, match="multi-factor"):
            await client.ensure_auth(force=True)
        await client.aclose()

    async def test_missing_token_raises_auth_error(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {("POST", "/api/v1/auth/login"): json_response({"message": "nope"})}
        )
        with pytest.raises(DonetickAuthError, match="did not contain a token"):
            await client.ensure_auth(force=True)
        await client.aclose()

    async def test_bad_password_surfaces_the_server_message(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {"error": "Invalid credentials"}, status=401
                )
            }
        )
        with pytest.raises(DonetickAuthError, match="Invalid credentials"):
            await client.ensure_auth(force=True)
        await client.aclose()


class TestExpiryParsing:
    def test_microsecond_expiry(self) -> None:
        stamp = DonetickClient._parse_expiry("2099-01-01T00:00:00.123456Z")
        assert stamp > datetime(2026, 1, 1).timestamp()

    def test_nanosecond_expiry_is_truncated(self) -> None:
        stamp = DonetickClient._parse_expiry("2099-01-01T00:00:00.123456789Z")
        assert stamp > datetime(2026, 1, 1).timestamp()

    def test_unparseable_expiry_falls_back_to_a_short_session(self) -> None:
        stamp = DonetickClient._parse_expiry("whenever")
        assert 0 < stamp - __import__("time").time() <= 301

    def test_missing_expiry_falls_back_to_a_short_session(self) -> None:
        assert DonetickClient._parse_expiry(None) > 0


# ---------------------------------------------------------------------------
# Retries and rate limiting
# ---------------------------------------------------------------------------


class TestRetries:
    async def test_retries_429_then_succeeds(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/"): [
                    json_response({"error": "slow down"}, status=429),
                    json_response({"res": []}),
                ]
            }
        )
        assert await client.list_chores() == []
        assert rec.paths().count("GET /api/v1/chores/") == 2
        await client.aclose()

    async def test_honours_retry_after_header(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/"): [
                    json_response({"error": "wait"}, status=429, headers={"Retry-After": "0.05"}),
                    json_response({"res": []}),
                ]
            }
        )
        start = __import__("time").monotonic()
        await client.list_chores()
        elapsed = __import__("time").monotonic() - start
        assert elapsed >= 0.04
        assert len(rec.paths()) == 2
        await client.aclose()

    async def test_retries_503(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/"): [
                    json_response({"error": "unavailable"}, status=503),
                    json_response({"res": []}),
                ]
            }
        )
        await client.list_chores()
        assert len(rec.paths()) == 2
        await client.aclose()

    async def test_does_not_retry_500(self, mock_client: Any) -> None:
        """500 is how Donetick reports 'Failed to retrieve chore'; retrying is pointless."""
        client, rec = mock_client(
            {("GET", "/api/v1/chores/999"): json_response({"error": "boom"}, status=500)}
        )
        with pytest.raises(DonetickError):
            await client.get_chore(999)
        assert len(rec.paths()) == 1
        await client.aclose()

    async def test_gives_up_after_max_retries(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/"): [
                    json_response({"error": "down"}, status=503) for _ in range(5)
                ]
            }
        )
        with pytest.raises(DonetickError):
            await client.list_chores()
        # max_retries=2 in the test config means 3 attempts in total.
        assert len(rec.paths()) == 3
        await client.aclose()

    async def test_backoff_is_jittered_and_capped(self) -> None:
        samples = {DonetickClient._backoff(attempt) for attempt in range(6) for _ in range(20)}
        assert len(samples) > 1  # jittered
        assert max(samples) <= 10.0


class TestReauthOn401:
    async def test_401_triggers_reauth_and_replay(self, mock_client: Any) -> None:
        """A revoked token must not permanently break the session."""
        client, rec = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {"access_token": "second-token", "access_token_expiry": "2099-01-01T00:00:00Z"}
                ),
                ("GET", "/api/v1/chores/"): [
                    json_response({"error": "unauthorized"}, status=401),
                    json_response({"res": []}),
                ],
            }
        )
        assert await client.list_chores() == []
        paths = rec.paths()
        assert paths[0] == "GET /api/v1/chores/"
        assert "POST /api/v1/auth/login" in paths
        assert paths[-1] == "GET /api/v1/chores/"
        await client.aclose()

    async def test_persistent_401_eventually_raises(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("POST", "/api/v1/auth/login"): json_response(
                    {"access_token": "still-bad", "access_token_expiry": "2099-01-01T00:00:00Z"}
                ),
                ("GET", "/api/v1/chores/"): json_response({"error": "no"}, status=401),
            }
        )
        with pytest.raises(DonetickError):
            await client.list_chores()
        await client.aclose()


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    async def test_error_key_on_200_becomes_an_exception(self, mock_client: Any) -> None:
        """`PUT /chores/{id}/start` answers 200 with an error body when invalid."""
        client, _ = mock_client(
            {
                ("PUT", "/api/v1/chores/5/start"): json_response(
                    {"error": "Chore is not in a state that can be started"}
                )
            }
        )
        with pytest.raises(DonetickError, match="not in a state that can be started"):
            await client.start_timer(5)
        await client.aclose()

    async def test_res_key_tolerated_alongside_message(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("PUT", "/api/v1/chores/5/archive"): json_response(
                    {"message": "archived", "res": {"id": 5}}
                )
            }
        )
        await client.archive_chore(5)  # must not raise
        await client.aclose()

    async def test_binding_details_are_included(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("POST", "/api/v1/chores/"): json_response(
                    {
                        "error": "Invalid request",
                        "details": "Key: 'ChoreReq.FrequencyType' Error:Field validation failed",
                    },
                    status=400,
                )
            }
        )
        with pytest.raises(DonetickError, match="ChoreReq.FrequencyType"):
            await client.create_chore(ChoreReq(name="x"))
        await client.aclose()

    async def test_404_raises_not_found(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {("DELETE", "/api/v1/labels/42"): json_response({"error": "no such label"}, status=404)}
        )
        with pytest.raises(DonetickNotFoundError, match="no such label"):
            await client.delete_label(42)
        await client.aclose()

    async def test_404_read_as_a_missing_chore(self, mock_client: Any) -> None:
        """`get_chore` normalises a 404 into `None` rather than raising."""
        client, _ = mock_client(
            {("GET", "/api/v1/chores/42"): json_response({"error": "not found"}, status=404)}
        )
        assert await client.get_chore(42) is None
        await client.aclose()

    async def test_missing_chore_via_500_returns_none(self, mock_client: Any) -> None:
        """Donetick answers a missing chore with 500 'Failed to retrieve chore'."""
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/999"): json_response(
                    {"error": "Failed to retrieve chore"}, status=500
                )
            }
        )
        assert await client.get_chore(999) is None
        await client.aclose()

    async def test_other_500_is_not_swallowed(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/999"): json_response(
                    {"error": "Database is on fire"}, status=500
                )
            }
        )
        with pytest.raises(DonetickError, match="on fire"):
            await client.get_chore(999)
        await client.aclose()

    async def test_error_key_on_200_is_read_as_missing_chore(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/999"): json_response(
                    {"error": "Failed to retrieve chore"}
                )
            }
        )
        assert await client.get_chore(999) is None
        await client.aclose()


# ---------------------------------------------------------------------------
# Endpoint shapes and paths
# ---------------------------------------------------------------------------


class TestRequestPaths:
    async def test_labels_endpoint_has_no_trailing_slash(self, mock_client: Any) -> None:
        """`/api/v1/labels/` 301-redirects and this client does not follow redirects."""
        client, rec = mock_client({("GET", "/api/v1/labels"): httpx.Response(200, json=[])})
        await client.list_labels()
        assert rec.paths() == ["GET /api/v1/labels"]
        await client.aclose()

    async def test_circle_members_has_no_trailing_slash(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {("GET", "/api/v1/circles/members"): json_response({"res": []})}
        )
        await client.get_circle_members()
        assert rec.paths() == ["GET /api/v1/circles/members"]
        await client.aclose()

    async def test_chores_list_has_trailing_slash(self, mock_client: Any) -> None:
        client, rec = mock_client({("GET", "/api/v1/chores/"): json_response({"res": []})})
        await client.list_chores()
        assert rec.paths() == ["GET /api/v1/chores/"]
        await client.aclose()

    async def test_users_list_has_trailing_slash(self, mock_client: Any) -> None:
        client, rec = mock_client({("GET", "/api/v1/users/"): json_response({"res": []})})
        await client.get_users()
        assert rec.paths() == ["GET /api/v1/users/"]
        await client.aclose()

    async def test_base_url_with_trailing_slash_does_not_double_up(self, mock_client: Any) -> None:
        client, rec = mock_client({("GET", "/api/v1/chores/"): json_response({"res": []})})
        client._base = client._base.rstrip("/")
        await client.list_chores()
        assert str(rec.requests[0].url) == "https://donetick.test/api/v1/chores/"
        await client.aclose()

    async def test_labels_list_parses_a_bare_array(self, mock_client: Any) -> None:
        """`GET /api/v1/labels` returns an array, not the usual {res: ...} envelope."""
        client, _ = mock_client(
            {
                ("GET", "/api/v1/labels"): httpx.Response(
                    200, json=[{"id": 1, "name": "cleaning", "color": "#fff"}]
                )
            }
        )
        labels = await client.list_labels()
        assert [label.name for label in labels] == ["cleaning"]
        await client.aclose()

    async def test_label_catalog_lowercases_names(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/labels"): httpx.Response(
                    200, json=[{"id": 1, "name": "Cleaning", "color": "#fff"}]
                )
            }
        )
        assert await client.get_label_catalog() == {"cleaning": 1}
        await client.aclose()

    async def test_create_label_posts_to_the_collection(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("POST", "/api/v1/labels"): json_response(
                    {"res": {"id": 3, "name": "outdoor", "color": "#0f0"}}
                )
            }
        )
        label = await client.create_label("outdoor", "#0f0")
        assert label.id == 3
        assert rec.last_json() == {"name": "outdoor", "color": "#0f0"}
        await client.aclose()

    async def test_delete_label_targets_the_id(self, mock_client: Any) -> None:
        client, rec = mock_client({("DELETE", "/api/v1/labels/4"): json_response({})})
        await client.delete_label(4)
        assert rec.paths() == ["DELETE /api/v1/labels/4"]
        await client.aclose()

    async def test_archived_chores_path(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {("GET", "/api/v1/chores/archived"): json_response({"res": []})}
        )
        await client.list_archived_chores()
        assert rec.paths() == ["GET /api/v1/chores/archived"]
        await client.aclose()

    async def test_approve_and_reject_use_post(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("POST", "/api/v1/chores/5/approve"): json_response({}),
                ("POST", "/api/v1/chores/5/reject"): json_response({}),
            }
        )
        await client.approve_chore(5)
        await client.reject_chore(5, reason="not done")
        assert rec.paths() == [
            "POST /api/v1/chores/5/approve",
            "POST /api/v1/chores/5/reject",
        ]
        assert rec.last_json() == {"notes": "not done"}
        await client.aclose()

    async def test_delete_chore_uses_http_delete(self, mock_client: Any) -> None:
        client, rec = mock_client({("DELETE", "/api/v1/chores/5"): json_response({})})
        await client.delete_chore(5)
        assert rec.paths() == ["DELETE /api/v1/chores/5"]
        await client.aclose()

    async def test_subtask_completion_sends_the_chore_id(self, mock_client: Any) -> None:
        client, rec = mock_client({("PUT", "/api/v1/chores/5/subtask"): json_response({})})
        await client.set_subtask_completion(5, 9, "2026-01-01T00:00:00Z")
        assert rec.last_json() == {
            "id": 9,
            "choreId": 5,
            "completedAt": "2026-01-01T00:00:00Z",
        }
        await client.aclose()

    async def test_subtask_can_be_cleared_with_null(self, mock_client: Any) -> None:
        client, rec = mock_client({("PUT", "/api/v1/chores/5/subtask"): json_response({})})
        await client.set_subtask_completion(5, 9, None)
        assert rec.last_json()["completedAt"] is None
        await client.aclose()


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------


class TestPayloads:
    async def test_create_sends_next_due_date_not_due_date(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {("POST", "/api/v1/chores/"): json_response({"res": 12})}
        )
        due = datetime(2026, 12, 25, 9, 0, tzinfo=UTC)
        chore_id = await client.create_chore(ChoreReq(name="x", next_due_date=due))
        assert chore_id == 12
        body = rec.last_json()
        assert "nextDueDate" in body
        assert "dueDate" not in body
        await client.aclose()

    async def test_create_returns_warnings_ignored(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {("POST", "/api/v1/chores/"): json_response({"res": 1, "warnings": ["x"]})}
        )
        assert await client.create_chore(ChoreReq(name="x")) == 1
        await client.aclose()

    async def test_create_rejects_a_non_integer_id(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {("POST", "/api/v1/chores/"): json_response({"res": "twelve"})}
        )
        with pytest.raises(DonetickError, match="valid chore id"):
            await client.create_chore(ChoreReq(name="x"))
        await client.aclose()

    async def test_update_strips_sync_version(self, mock_client: Any) -> None:
        """Sending `syncVersion` back trips a 403 conflict check."""
        client, rec = mock_client(
            {("PUT", "/api/v1/chores/"): json_response({"message": "ok"})}
        )
        await client.update_chore(ChoreReq(id=5, name="x", priority=1))
        assert "syncVersion" not in rec.last_json()
        await client.aclose()

    async def test_update_omits_the_optimistic_lock(
        self, mock_client: Any
    ) -> None:
        """`ChoreReq` has no `updatedAt`, and adding one would cause 403s.

        Donetick's `CanEdit` compares the sent `updatedAt` against the chore's
        server-side `UpdatedAt`, which is always a hair newer than any value a
        client can read -- so echoing it back is rejected too.
        """
        client, rec = mock_client(
            {("PUT", "/api/v1/chores/"): json_response({"message": "ok"})}
        )
        req = ChoreReq(id=5, name="x")
        assert not hasattr(req, "updated_at")
        await client.update_chore(req)
        assert "updatedAt" not in rec.last_json()
        await client.aclose()

    async def test_update_always_sends_labels_and_subtasks(
        self, mock_client: Any
    ) -> None:
        """Regression: Donetick dereferences both pointers without a nil check.

        Omitting `labelsV2` or `subTasks` panics the server, which drops the
        connection with no HTTP response at all.
        """
        client, rec = mock_client(
            {("PUT", "/api/v1/chores/"): json_response({"message": "ok"})}
        )
        await client.update_chore(ChoreReq(id=5, name="x"))
        body = rec.last_json()
        assert body["labelsV2"] == []
        assert body["subTasks"] == []
        await client.aclose()

    async def test_update_keeps_existing_labels_and_subtasks(
        self, mock_client: Any
    ) -> None:
        """The payload is a full replacement, so existing children must survive."""
        client, rec = mock_client(
            {("PUT", "/api/v1/chores/"): json_response({"message": "ok"})}
        )
        req = ChoreReq(
            id=5,
            name="x",
            labels_v2=[Label(id=3, label_id=3, name="outdoor")],
            sub_tasks=[SubTask(name="mop")],
        )
        await client.update_chore(req)
        body = rec.last_json()
        assert body["labelsV2"][0]["labelId"] == 3
        assert body["subTasks"][0]["name"] == "mop"
        await client.aclose()

    async def test_update_returns_warnings(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("PUT", "/api/v1/chores/"): json_response(
                    {"message": "ok", "warnings": ["isPrivate not provided"]}
                )
            }
        )
        warnings = await client.update_chore(ChoreReq(id=5, name="x"))
        assert warnings == ["isPrivate not provided"]
        await client.aclose()

    async def test_update_due_date_goes_through_the_main_put(self, mock_client: Any) -> None:
        """Regression: the standalone `/dueDate` route 403s on current Donetick."""
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/5"): json_response(
                    {
                        "res": {
                            "id": 5,
                            "name": "x",
                            "nextDueDate": "2026-01-01T10:00:00Z",
                        }
                    }
                ),
                ("PUT", "/api/v1/chores/"): json_response({}),
            }
        )
        await client.update_due_date(5, datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        body = rec.last_json()
        assert body["nextDueDate"] == "2026-03-01T09:00:00Z"
        # The field name current Donetick ignores must never reappear.
        assert "dueDate" not in body
        await client.aclose()

    async def test_update_due_date_restores_a_rolling_chore_missing_one(
        self, mock_client: Any
    ) -> None:
        """A rolling chore that lost its date must remain editable."""
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/5"): json_response(
                    {"res": {"id": 5, "name": "x", "isRolling": True}}
                ),
                ("PUT", "/api/v1/chores/"): json_response({}),
            }
        )
        await client.update_due_date(5, datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        body = rec.last_json()
        assert body["nextDueDate"] == "2026-03-01T09:00:00Z"
        assert body["isRolling"] is True
        await client.aclose()

    async def test_update_payload_keeps_explicit_nulls(self, mock_client: Any) -> None:
        """The PUT is a full replacement: a dropped key is a reset server-side."""
        client, rec = mock_client({("PUT", "/api/v1/chores/"): json_response({})})
        await client.update_chore(
            ChoreReq(id=5, name="x", points=None, assigned_to=None, next_due_date=None)
        )
        body = rec.last_json()
        assert "points" in body and body["points"] is None
        assert "assignedTo" in body and body["assignedTo"] is None
        assert "nextDueDate" in body and body["nextDueDate"] is None
        # Still guarded: Donetick dereferences these two without a nil check.
        assert body["labelsV2"] == []
        assert body["subTasks"] == []
        await client.aclose()

    async def test_update_due_date_raises_for_a_missing_chore(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/999"): json_response(
                    {"error": "Failed to retrieve chore"}, status=500
                )
            }
        )
        with pytest.raises(DonetickNotFoundError, match="not found"):
            await client.update_due_date(999, datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        await client.aclose()

    async def test_complete_sends_the_note_under_both_keys(self, mock_client: Any) -> None:
        """Donetick's `do` handler reads `notes`; older builds read `note`."""
        client, rec = mock_client(
            {("POST", "/api/v1/chores/5/do"): json_response({"res": {"id": 5, "name": "x"}})}
        )
        await client.complete_chore(5, note="all done")
        assert rec.last_json() == {"note": "all done", "notes": "all done"}
        await client.aclose()

    async def test_skip_sends_the_note(self, mock_client: Any) -> None:
        client, rec = mock_client({("POST", "/api/v1/chores/5/skip"): json_response({})})
        await client.skip_chore(5, note="away")
        assert rec.last_json() == {"notes": "away"}
        await client.aclose()

    async def test_chore_to_req_preserves_assignees_and_labels(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/5"): json_response(
                    {
                        "res": {
                            "id": 5,
                            "name": "x",
                            "frequencyType": "weekly",
                            "frequency": 2,
                            "assignees": [{"userId": 3}, {"userId": 4}],
                            "assignedTo": 3,
                            "labelsV2": [{"id": 9, "name": "outdoor", "color": "#0f0"}],
                            "subTasks": [{"id": 1, "orderId": 0, "name": "front"}],
                            "description": "desc",
                        }
                    }
                )
            }
        )
        chore = await client.get_chore(5)
        assert chore is not None
        req = await client.chore_to_req(chore)
        assert [a.user_id for a in req.assignees] == [3, 4]
        assert req.labels_v2 is not None
        assert req.labels_v2[0].label_id == 9
        assert req.sub_tasks is not None
        assert req.sub_tasks[0].name == "front"
        assert req.description == "desc"
        await client.aclose()

    async def test_chore_to_req_defaults_a_zero_frequency(self, mock_client: Any) -> None:
        """A stored frequency of 0 would fail the model's minimum-1 check."""
        client, _ = mock_client(
            {
                ("GET", "/api/v1/chores/5"): json_response(
                    {"res": {"id": 5, "name": "x", "frequency": 0}}
                )
            }
        )
        chore = await client.get_chore(5)
        assert chore is not None
        req = await client.chore_to_req(chore)
        assert req.frequency == 1
        await client.aclose()

    async def test_replace_subtasks_sends_the_whole_list(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/5"): json_response(
                    {
                        "res": {
                            "id": 5,
                            "name": "x",
                            "subTasks": [
                                {"id": 1, "orderId": 0, "name": "front"},
                                {"id": 2, "orderId": 1, "name": "back"},
                            ],
                        }
                    }
                ),
                ("PUT", "/api/v1/chores/"): json_response({"message": "ok"}),
            }
        )
        chore = await client.get_chore(5)
        assert chore is not None
        await client.replace_subtasks(chore, [chore.sub_tasks[0]])  # type: ignore[index]
        assert [t["name"] for t in rec.last_json()["subTasks"]] == ["front"]
        await client.aclose()


# ---------------------------------------------------------------------------
# Create recovery
# ---------------------------------------------------------------------------


class TestCreateRecovery:
    async def test_recovers_a_chore_created_before_a_disconnect(
        self, mock_client: Any
    ) -> None:
        """Donetick sometimes drops the connection after committing the write."""

        def disconnect_then_recover(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/api/v1/chores/":
                raise httpx.RemoteProtocolError("peer closed")
            return json_response(
                {
                    "res": [
                        {
                            "id": 41,
                            "name": "x",
                            "nextDueDate": "2026-12-25T09:00:00Z",
                            "assignedTo": 3,
                        }
                    ]
                }
            )

        client, _ = mock_client(
            {
                ("POST", "/api/v1/chores/"): disconnect_then_recover,
                ("GET", "/api/v1/chores/"): disconnect_then_recover,
            }
        )
        req = ChoreReq(
            name="x",
            next_due_date=datetime(2026, 12, 25, 9, 0, tzinfo=UTC),
            assigned_to=3,
            assignees=[ChoreAssignees(user_id=3)],
        )
        assert await client.create_chore(req) == 41
        await client.aclose()

    async def test_disconnect_without_a_due_date_is_retried(self, mock_client: Any) -> None:
        """With no due date there is nothing to match on, so retry instead.

        Recovery is deliberately skipped: matching on name alone could match a
        pre-existing chore and return the wrong ID.
        """

        def always_disconnect(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("peer closed")

        client, rec = mock_client({("POST", "/api/v1/chores/"): always_disconnect})
        with pytest.raises(DonetickError):
            await client.create_chore(ChoreReq(name="x"))
        # Retried, and no recovery lookup was issued.
        assert len(rec.paths()) == 3  # max_retries=2 -> 3 attempts
        assert not any(path.endswith("/chores/") and path.startswith("GET") for path in rec.paths())
        await client.aclose()

    async def test_disconnect_with_nothing_to_match_raises(
        self, mock_client: Any
    ) -> None:
        def disconnect(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                raise httpx.RemoteProtocolError("peer closed")
            return json_response({"res": []})

        client, _ = mock_client(
            {
                ("POST", "/api/v1/chores/"): disconnect,
                ("GET", "/api/v1/chores/"): disconnect,
            }
        )
        with pytest.raises(DonetickTransportError):
            await client.create_chore(
                ChoreReq(
                    name="nothing-matches",
                    next_due_date=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        await client.aclose()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestMisc:
    async def test_profile_is_parsed(self, mock_client: Any) -> None:
        client, _ = mock_client(
            {
                ("GET", "/api/v1/users/profile"): json_response(
                    {"res": {"id": 3, "displayName": "Sam", "username": "sam", "circleId": 1}}
                )
            }
        )
        profile = await client.get_profile()
        assert profile.id == 3
        assert profile.display_name == "Sam"
        await client.aclose()

    async def test_history_parses_entries(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {
                ("GET", "/api/v1/chores/5/history"): json_response(
                    {
                        "res": [
                            {
                                "id": 1,
                                "choreId": 5,
                                "performedAt": "2026-01-01T00:00:00Z",
                                "completedBy": 3,
                            }
                        ]
                    }
                )
            }
        )
        entries = await client.get_chore_history(5, 14)
        assert entries[0].completed_by == 3
        assert "duration=14" in str(rec.requests[0].url)
        await client.aclose()

    async def test_is_recurring(self) -> None:
        assert DonetickClient.is_recurring(FrequencyType.DAILY) is True
        assert DonetickClient.is_recurring(FrequencyType.ONCE) is False
        assert DonetickClient.is_recurring(FrequencyType.NO_REPEAT) is False

    async def test_config_requires_credentials(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DONETICK_USERNAME", raising=False)
        monkeypatch.delenv("DONETICK_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="required"):
            Config.from_env()


class TestConfigFromEnv:
    def test_defaults(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        for name in (
            "DONETICK_URL",
            "DONETICK_TIMEOUT",
            "DONETICK_TIMEZONE",
            "DONETICK_RATE_LIMIT_PER_SECOND",
            "DONETICK_RATE_LIMIT_BURST",
            "DONETICK_MAX_RETRIES",
            "DONETICK_VERIFY_TLS",
        ):
            monkeypatch.delenv(name, raising=False)
        config = Config.from_env()
        assert config.url == "https://donetick.com"
        assert config.timezone == "UTC"
        assert config.timeout == 10.0
        assert config.max_retries == 3

    def test_timezone_is_configurable(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_TIMEZONE", "America/New_York")
        assert Config.from_env().timezone == "America/New_York"

    def test_trailing_slash_is_stripped(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_URL", "https://donetick.example.com///")
        assert Config.from_env().url == "https://donetick.example.com"

    def test_rate_limit_settings(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_RATE_LIMIT_PER_SECOND", "2.5")
        monkeypatch.setenv("DONETICK_RATE_LIMIT_BURST", "5")
        config = Config.from_env()
        assert config.rate_limit_per_second == 2.5
        assert config.rate_limit_burst == 5

    def test_bad_numeric_setting_is_rejected(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_TIMEOUT", "soon")
        with pytest.raises(RuntimeError, match="must be a number"):
            Config.from_env()

    def test_non_positive_timeout_is_rejected(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_TIMEOUT", "0")
        with pytest.raises(RuntimeError, match="greater than 0"):
            Config.from_env()

    @pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
    def test_truthy_tls_settings(self, monkeypatch: Any, raw: str) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_VERIFY_TLS", raw)
        assert Config.from_env().verify_tls is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
    def test_falsy_tls_settings(self, monkeypatch: Any, raw: str) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_VERIFY_TLS", raw)
        assert Config.from_env().verify_tls is False

    def test_bad_boolean_is_rejected(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.setenv("DONETICK_PASSWORD", "p")
        monkeypatch.setenv("DONETICK_VERIFY_TLS", "maybe")
        with pytest.raises(RuntimeError, match="must be a boolean"):
            Config.from_env()

    def test_missing_password_is_reported(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DONETICK_USERNAME", "u")
        monkeypatch.delenv("DONETICK_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="DONETICK_PASSWORD"):
            Config.from_env()


class TestHistoryQueryParams:
    async def test_circle_history_includes_flag(self, mock_client: Any) -> None:
        client, rec = mock_client(
            {("GET", "/api/v1/chores/history"): json_response({"res": []})}
        )
        await client.get_history(duration_days=30, include_circle=True)
        url = str(rec.requests[0].url)
        assert "limit=30" in url
        assert "includeCircle=true" in url
        await client.aclose()

    async def test_serialised_payload_uses_camel_aliases(self, mock_client: Any) -> None:
        """A final guard on the field-name bug that motivated this rewrite."""
        client, rec = mock_client(
            {("POST", "/api/v1/chores/"): json_response({"res": 1})}
        )
        await client.create_chore(
            ChoreReq(
                name="x",
                frequency_type=FrequencyType.DAYS_OF_THE_WEEK,
                frequency=1,
                next_due_date=datetime(2026, 12, 7, 19, 0, tzinfo=UTC),
                is_rolling=False,
                )
        )
        body = json.loads(rec.bodies[-1])
        assert body["frequencyType"] == "days_of_the_week"
        assert body["nextDueDate"].startswith("2026-12-07T19:00:00")
        assert "dueDate" not in body
        await client.aclose()
