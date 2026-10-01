"""Tests for the FastMCP tool and resource layer.

The tools are exercised in-process through FastMCP's own client against an
in-memory MCP server, so argument handling, response shaping and error
translation are all covered without a network round-trip.  The Donetick binary
is never started.

``DONETICK_*`` environment variables are set so ``Config.from_env()`` succeeds
normally; only the HTTP transport is replaced, via a stub client class
substituted into the server module.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastmcp import Client

from donetick_mcp import Config, DonetickClient
from donetick_mcp.server import mcp

def _payload(result: Any) -> Any:
    """Unwrap a FastMCP tool/resource result into plain JSON.

    Prefers the text content blocks so the value is exactly what the tool or
    resource produced, with no re-validation by the client.  Falls back to
    ``structured_content`` for results FastMCP serialises structurally (an
    empty list, for instance, carries no text block).
    """
    blocks = result if isinstance(result, list) else [result]
    texts: list[Any] = []
    for block in blocks:
        for item in getattr(block, "content", None) or [block]:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(json.loads(text))
    if texts:
        return texts[0] if len(texts) == 1 else texts

    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    data = getattr(result, "data", None)
    if data is not None:
        return data if not hasattr(data, "model_dump") else data.model_dump()
    raise AssertionError(f"no decodable content in result: {result!r}")


CHORE_ID = 42
NOW = "2026-12-25T09:00:00Z"

CHORE_FIXTURE: dict[str, Any] = {
    "id": CHORE_ID,
    "name": "Mow lawn",
    "description": "Front and back",
    "frequency": 1,
    "frequencyType": "days_of_the_week",
    "frequencyMetadata": {
        "days": ["monday", "thursday"],
        "unit": "days",
        "weekPattern": "every_week",
        "timezone": "UTC",
    },
    "nextDueDate": NOW,
    "isRolling": False,
    "assignedTo": 1,
    "assignees": [{"userId": 1}],
    "assignStrategy": "keep_last_assigned",
    "isActive": True,
    "notification": False,
    "labelsV2": [{"id": 1, "name": "outdoor", "color": "#0f0"}],
    "circleId": 1,
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-02T00:00:00Z",
    "status": 0,
    "priority": 2,
    "points": 5,
    "requireApproval": False,
    "isPrivate": False,
    "syncVersion": 3,
    "subTasks": [
        {"id": 1, "orderId": 0, "name": "front", "completedAt": None},
        {
            "id": 2,
            "orderId": 1,
            "name": "back",
            "completedAt": "2026-01-01T00:00:00Z",
        },
    ],
}

PROFILE_FIXTURE: dict[str, Any] = {
    "id": 1,
    "displayName": "Test User",
    "email": "test@example.com",
    "username": "testuser",
    "circleId": 1,
}

MEMBER_FIXTURE: dict[str, Any] = {
    "userId": 1,
    "username": "testuser",
    "displayName": "Test User",
    "role": "owner",
    "isActive": True,
    "points": 7,
    "pointsRedeemed": 0,
}

LABEL_FIXTURE: dict[str, Any] = {"id": 1, "name": "outdoor", "color": "#0f0"}

Key = tuple[str, str]


def ok(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def routes(**overrides: Any) -> dict[Key, Any]:
    """Default mock routes, overridable per test via ``routes(...)``."""
    table: dict[Key, Any] = {
        ("GET", "/api/v1/users/profile"): ok({"res": PROFILE_FIXTURE}),
        ("GET", "/api/v1/users/"): ok({"res": [PROFILE_FIXTURE]}),
        ("GET", "/api/v1/circles/members"): ok({"res": [MEMBER_FIXTURE]}),
        ("GET", "/api/v1/labels"): ok([LABEL_FIXTURE]),
        ("GET", "/api/v1/chores/"): ok({"res": [CHORE_FIXTURE]}),
        ("GET", "/api/v1/chores/archived"): ok({"res": []}),
        ("GET", f"/api/v1/chores/{CHORE_ID}"): ok({"res": CHORE_FIXTURE}),
        ("POST", "/api/v1/chores/"): ok({"res": 99}),
        ("PUT", "/api/v1/chores/"): ok({"message": "Chore updated successfully"}),
        ("POST", f"/api/v1/chores/{CHORE_ID}/do"): ok({"res": CHORE_FIXTURE}),
        ("POST", f"/api/v1/chores/{CHORE_ID}/skip"): ok({}),
        ("POST", f"/api/v1/chores/{CHORE_ID}/approve"): ok({}),
        ("POST", f"/api/v1/chores/{CHORE_ID}/reject"): ok({}),
        ("PUT", f"/api/v1/chores/{CHORE_ID}/archive"): ok({"message": "archived"}),
        ("PUT", f"/api/v1/chores/{CHORE_ID}/unarchive"): ok({"message": "restored"}),
        ("DELETE", f"/api/v1/chores/{CHORE_ID}"): ok({}),
        ("PUT", f"/api/v1/chores/{CHORE_ID}/dueDate"): ok({}),
        ("PUT", f"/api/v1/chores/{CHORE_ID}/subtask"): ok({}),
        ("GET", f"/api/v1/chores/{CHORE_ID}/history"): ok({"res": []}),
        ("GET", "/api/v1/chores/history"): ok({"res": []}),
        ("POST", "/api/v1/labels"): ok({"res": LABEL_FIXTURE}),
        ("PUT", "/api/v1/labels"): ok({"res": LABEL_FIXTURE}),
        ("DELETE", "/api/v1/labels/1"): ok({"res": "Label deleted"}),
    }
    table.update(overrides)
    return table


class Harness:
    """A running in-memory MCP server plus the requests it made."""

    def __init__(self, table: dict[Key, Any]) -> None:
        self.table = table
        self.requests: list[httpx.Request] = []

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.table.get((request.method, request.url.path))
        if reply is None:
            return ok({"error": f"no mock for {request.method} {request.url.path}"}, 404)
        if isinstance(reply, list):
            return reply.pop(0) if reply else ok({"error": "exhausted"}, 500)
        if callable(reply):
            return reply(request)
        return reply

    def body_for(self, method: str, path: str) -> Any:
        for request in self.requests:
            if request.method == method and request.url.path == path:
                if request.content:
                    return json.loads(request.content)
        return None

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a factory building an MCP client bound to a mock transport.

    Usage::

        async with harness(routes(...)) as h:
            result = await h.call("list_chores")
    """
    import contextlib

    import donetick_mcp.server as server_module

    monkeypatch.setenv("DONETICK_URL", "https://donetick.test")
    monkeypatch.setenv("DONETICK_USERNAME", "testuser")
    monkeypatch.setenv("DONETICK_PASSWORD", "testpassword123")
    monkeypatch.setenv("DONETICK_TIMEZONE", "UTC")
    monkeypatch.setenv("DONETICK_RATE_LIMIT_PER_SECOND", "1000")
    monkeypatch.setenv("DONETICK_RATE_LIMIT_BURST", "1000")
    monkeypatch.setenv("DONETICK_MAX_RETRIES", "1")

    real_client = server_module.DonetickClient

    @contextlib.asynccontextmanager
    async def _harness(table: dict[Key, Any] | None = None) -> AsyncIterator[Harness]:
        instance = Harness(table if table is not None else routes())

        def factory(config: Config, **_: Any) -> DonetickClient:
            http = httpx.AsyncClient(
                transport=httpx.MockTransport(instance.dispatch),
                follow_redirects=False,
            )
            client = real_client(config, client=http)
            # Pre-seed auth so the lifespan's ensure_auth() is a no-op.
            client._token = "test-token"
            client._token_expire = float("inf")
            return client

        monkeypatch.setattr(server_module, "DonetickClient", factory)
        async with Client(mcp) as client:
            instance.client = client  # type: ignore[attr-defined]
            yield instance

    return _harness


class TestToolRegistration:
    """The advertised tool surface."""

    async def test_core_tools_present(self, harness: Any) -> None:
        async with harness() as h:
            names = {tool.name for tool in await h.client.list_tools()}
        for expected in (
            "list_chores",
            "search_chores",
            "get_chore",
            "create_chore",
            "update_chore",
            "complete_chore",
            "archive_chore",
            "unarchive_chore",
            "delete_chore",
            "skip_chore",
            "approve_chore",
            "reject_chore",
            "update_due_date",
            "list_archived_chores",
            "start_chore_timer",
            "pause_chore_timer",
            "list_subtasks",
            "create_subtask",
            "delete_subtask",
            "update_subtask_completion",
            "list_labels",
            "create_label",
            "update_label",
            "delete_label",
            "get_chore_history",
            "get_history",
            "get_profile",
            "list_users",
        ):
            assert expected in names

    async def test_resources_present(self, harness: Any) -> None:
        async with harness() as h:
            uris = {str(r.uri) for r in await h.client.list_resources()}
            templates = {
                str(t.uriTemplate)
                for t in await h.client.list_resource_templates()
            }
        assert "donetick://users" in uris
        assert "donetick://labels" in uris
        assert "donetick://profile" in uris
        assert "donetick://chores" in uris
        assert any("chore/{chore_id}" in t for t in templates)

    async def test_every_tool_documents_its_arguments(self, harness: Any) -> None:
        """Tool docstrings drive the generated schemas; empty ones hide params."""
        async with harness() as h:
            tools = await h.client.list_tools()
        for tool in tools:
            if not tool.description:
                continue
            assert tool.description.strip()
            assert "Args:" not in tool.description or len(tool.description) > 40


class TestListAndSearch:
    async def test_list_chores_returns_summaries(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("list_chores", {})
        data = _payload(result)
        assert data[0]["id"] == CHORE_ID
        assert data[0]["assignees"] == [1]
        assert data[0]["frequency_type"] == "days_of_the_week"

    async def test_list_chores_can_include_archived(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool("list_chores", {"include_archived": True})
        assert "GET /api/v1/chores/archived" in h.paths()

    async def test_list_archived_tool(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool("list_archived_chores", {})
        assert "GET /api/v1/chores/archived" in h.paths()

    async def test_search_matches_by_name(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("search_chores", {"query": "mow"})
        assert _payload(result)[0]["name"] == "Mow lawn"

    async def test_search_matches_by_label(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("search_chores", {"query": "outdoor"})
        assert len(_payload(result)) == 1

    async def test_search_miss_returns_empty(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("search_chores", {"query": "zzz_nope"})
        assert _payload(result) == []

    async def test_list_chores_reports_errors_instead_of_raising(
        self, harness: Any
    ) -> None:
        table = routes()
        table[("GET", "/api/v1/chores/")] = ok({"error": "db exploded"}, 500)
        async with harness(table) as h:
            result = await h.client.call_tool("list_chores", {})
        assert "db exploded" in _payload(result)["error"]


class TestGetChore:
    async def test_returns_full_detail(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("get_chore", {"chore_id": CHORE_ID})
        data = _payload(result)
        assert data["id"] == CHORE_ID
        assert data["labels"] == ["outdoor"]
        assert data["sub_tasks"][1]["completed"] is True
        assert data["frequency_metadata"]["weekPattern"] == "every_week"

    async def test_missing_chore_is_reported(self, harness: Any) -> None:
        table = routes()
        table[("GET", "/api/v1/chores/999")] = ok(
            {"error": "Failed to retrieve chore"}, 500
        )
        async with harness(table) as h:
            result = await h.client.call_tool("get_chore", {"chore_id": 999})
        assert "not found" in _payload(result)["error"]


class TestCreateChore:
    async def test_minimal_create_sends_a_valid_payload(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("create_chore", {"name": "New chore"})
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["name"] == "New chore"
        assert body["frequency"] == 1
        assert "dueDate" not in body
        assert "nextDueDate" not in body
        assert _payload(result)["id"] == 99

    async def test_due_date_is_sent_as_next_due_date(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore", {"name": "x", "due_date": "2026-12-25T09:00:00Z"}
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["nextDueDate"] == "2026-12-25T09:00:00Z"
        assert "dueDate" not in body

    async def test_bare_date_is_parsed(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool("create_chore", {"name": "x", "due_date": "2026-12-25"})
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["nextDueDate"].startswith("2026-12-25T00:00:00")

    async def test_unparseable_due_date_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "due_date": "next tuesday"}
            )
        assert "error" in _payload(result)

    async def test_zero_frequency_is_rejected_locally(self, harness: Any) -> None:
        """Rejected before the request, since 0 silently breaks rescheduling."""
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "frequency": 0}
            )
        assert "frequency must be at least 1" in _payload(result)["error"]
        assert h.requests == []

    async def test_assigned_to_is_added_to_assignees(self, harness: Any) -> None:
        """Donetick 400s when `assignedTo` is missing from `assignees`."""
        async with harness() as h:
            await h.client.call_tool(
                "create_chore",
                {"name": "x", "assigned_to": 1, "assignees": []},
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert [a["userId"] for a in body["assignees"]] == [1]

    async def test_username_assignment_is_resolved(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore",
                {"name": "x", "assigned_username": "testuser", "assign_strategy": "keep_last_assigned"},
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["assignedTo"] == 1
        assert body["assignStrategy"] == "keep_last_assigned"

    async def test_unknown_username_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "assigned_username": "nobody"}
            )
        assert "Unknown user" in _payload(result)["error"]

    async def test_days_of_week_builds_metadata(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore",
                {
                    "name": "x",
                    "frequency_type": "days_of_the_week",
                    "days_of_week": ["mon", "thu"],
                },
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["frequencyMetadata"]["days"] == ["monday", "thursday"]
        assert body["frequencyMetadata"]["timezone"] == "UTC"
        assert body["frequencyMetadata"]["weekPattern"] == "every_week"

    async def test_days_of_the_week_without_days_is_reported(
        self, harness: Any
    ) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "frequency_type": "days_of_the_week"}
            )
        assert "days_of_week is required" in _payload(result)["error"]

    async def test_invalid_day_name_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore",
                {"name": "x", "frequency_type": "days_of_the_week", "days_of_week": ["funday"]},
            )
        assert "Invalid day name" in _payload(result)["error"]

    async def test_labels_are_resolved_to_ids(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore", {"name": "x", "labels": ["outdoor"]}
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["labelsV2"] == [
            {"id": 1, "labelId": 1, "name": "outdoor"},
        ]

    async def test_unknown_label_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "labels": ["nonexistent"]}
            )
        assert "Unknown label" in _payload(result)["error"]

    async def test_subtasks_are_built(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore", {"name": "x", "subtasks": ["one", "two"]}
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert [t["name"] for t in body["subTasks"]] == ["one", "two"]
        assert [t["orderId"] for t in body["subTasks"]] == [0, 1]

    async def test_reminders_enable_notification(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore", {"name": "x", "remind_minutes_before": 15}
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["notification"] is True
        assert body["notificationMetadata"]["templates"] == [
            {"value": 15, "unit": "m"}
        ]

    async def test_rolling_without_due_date_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_chore", {"name": "x", "is_rolling": True}
            )
        assert "next_due_date is required" in _payload(result)["error"]

    async def test_rotation_without_assignees_is_downgraded(
        self, harness: Any
    ) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "create_chore",
                {"name": "x", "assign_strategy": "round_robin", "assignees": []},
            )
        body = h.body_for("POST", "/api/v1/chores/")
        assert body["assignStrategy"] == "no_assignee"

    async def test_server_error_is_reported(self, harness: Any) -> None:
        table = routes()
        table[("POST", "/api/v1/chores/")] = ok(
            {"error": "Invalid request", "details": "frequencyType required"}, 400
        )
        async with harness(table) as h:
            result = await h.client.call_tool("create_chore", {"name": "x"})
        error = _payload(result)["error"]
        assert "Invalid request" in error
        assert "frequencyType required" in error


class TestUpdateChore:
    async def test_only_the_named_field_changes(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "update_chore", {"chore_id": CHORE_ID, "name": "Renamed"}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert body["name"] == "Renamed"
        # Everything else is carried over from the fetched chore.
        assert body["description"] == "Front and back"
        assert body["priority"] == 2
        assert body["nextDueDate"] == NOW
        assert _payload(result)["message"].startswith("Chore 42 updated")

    async def test_sync_version_is_never_sent_back(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore", {"chore_id": CHORE_ID, "name": "Renamed"}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert "syncVersion" not in body

    async def test_due_date_update(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore",
                {"chore_id": CHORE_ID, "due_date": "2027-01-15T10:00:00Z"},
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        # Pydantic renders an aware UTC datetime with a literal `Z`.
        assert body["nextDueDate"] == "2027-01-15T10:00:00Z"

    async def test_assignment_switch(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore",
                {
                    "chore_id": CHORE_ID,
                    "assign_strategy": "round_robin",
                    "assignee_usernames": ["testuser"],
                },
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert body["assignStrategy"] == "round_robin"
        assert [a["userId"] for a in body["assignees"]] == [1]

    async def test_add_and_remove_labels(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore",
                {"chore_id": CHORE_ID, "remove_labels": ["outdoor"], "add_labels": ["outdoor"]},
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert [label["name"] for label in body["labelsV2"]] == ["outdoor"]

    async def test_remove_a_label_persists(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore", {"chore_id": CHORE_ID, "set_labels": []}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert body["labelsV2"] == []

    async def test_add_and_remove_subtasks(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "update_chore",
                {
                    "chore_id": CHORE_ID,
                    "add_subtasks": ["side"],
                    "remove_subtasks": ["back"],
                },
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert [t["name"] for t in body["subTasks"]] == ["front", "side"]

    async def test_missing_chore_is_reported(self, harness: Any) -> None:
        table = routes()
        table[("GET", "/api/v1/chores/999")] = ok(
            {"error": "Failed to retrieve chore"}, 500
        )
        async with harness(table) as h:
            result = await h.client.call_tool(
                "update_chore", {"chore_id": 999, "name": "x"}
            )
        assert "not found" in _payload(result)["error"]

    async def test_warnings_are_surfaced(self, harness: Any) -> None:
        table = routes()
        table[("PUT", "/api/v1/chores/")] = ok(
            {"message": "ok", "warnings": ["isPrivate not provided"]}
        )
        async with harness(table) as h:
            result = await h.client.call_tool(
                "update_chore", {"chore_id": CHORE_ID, "name": "x"}
            )
        assert "isPrivate not provided" in _payload(result)["message"]


class TestLifecycleTools:
    async def test_complete_returns_the_updated_chore(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "complete_chore", {"chore_id": CHORE_ID, "note": "done"}
            )
        assert h.body_for("POST", f"/api/v1/chores/{CHORE_ID}/do") == {
            "note": "done",
            "notes": "done",
        }
        assert _payload(result)["id"] == CHORE_ID

    async def test_skip(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "skip_chore", {"chore_id": CHORE_ID, "note": "away"}
            )
        assert h.body_for("POST", f"/api/v1/chores/{CHORE_ID}/skip") == {
            "notes": "away"
        }
        assert "skipped" in _payload(result)["message"]

    async def test_approve(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("approve_chore", {"chore_id": CHORE_ID})
        assert f"POST /api/v1/chores/{CHORE_ID}/approve" in h.paths()
        assert "approved" in _payload(result)["message"]

    async def test_reject(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "reject_chore", {"chore_id": CHORE_ID, "reason": "not done"}
            )
        assert h.body_for("POST", f"/api/v1/chores/{CHORE_ID}/reject") == {
            "notes": "not done"
        }
        assert "rejected" in _payload(result)["message"]

    async def test_archive_warns_that_it_is_reversible(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("archive_chore", {"chore_id": CHORE_ID})
        assert "unarchive_chore" in _payload(result)["message"]

    async def test_unarchive(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("unarchive_chore", {"chore_id": CHORE_ID})
        assert "restored" in _payload(result)["message"]

    async def test_update_due_date_uses_the_dedicated_endpoint(
        self, harness: Any
    ) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "update_due_date", {"chore_id": CHORE_ID, "due_date": "2027-02-01T08:00:00Z"}
            )
        body = h.body_for("PUT", f"/api/v1/chores/{CHORE_ID}/dueDate")
        assert body["dueDate"].startswith("2027-02-01T08:00:00")
        # Donetick binds updatedAt as required.
        assert body["updatedAt"] == "2026-01-02T00:00:00Z"
        assert _payload(result)["message"]

    async def test_update_due_date_rejects_an_empty_value(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "update_due_date", {"chore_id": CHORE_ID, "due_date": ""}
            )
        assert "must not be empty" in _payload(result)["error"]


class TestDeleteChore:
    async def test_refuses_without_confirmation(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("delete_chore", {"chore_id": CHORE_ID})
        assert "confirm=True" in _payload(result)["error"]
        assert not any("DELETE" in path for path in h.paths())

    async def test_deletes_when_confirmed(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "delete_chore", {"chore_id": CHORE_ID, "confirm": True}
            )
        assert f"DELETE /api/v1/chores/{CHORE_ID}" in h.paths()
        assert "permanently deleted" in _payload(result)["message"]


class TestTimerTools:
    async def test_start(self, harness: Any) -> None:
        table = routes()
        table[("PUT", f"/api/v1/chores/{CHORE_ID}/start")] = ok(
            {"res": {"duration": 0, "status": 1, "timerUpdatedAt": NOW}}
        )
        async with harness(table) as h:
            result = await h.client.call_tool("start_chore_timer", {"chore_id": CHORE_ID})
        data = _payload(result)
        assert data["chore_id"] == CHORE_ID
        assert data["chore_status_label"] == "in_progress"

    async def test_pause(self, harness: Any) -> None:
        table = routes()
        table[("PUT", f"/api/v1/chores/{CHORE_ID}/pause")] = ok(
            {"res": {"duration": 120, "status": 2, "timerUpdatedAt": NOW}}
        )
        async with harness(table) as h:
            result = await h.client.call_tool("pause_chore_timer", {"chore_id": CHORE_ID})
        data = _payload(result)
        assert data["duration"] == 120
        assert data["chore_status_label"] == "paused"

    async def test_error_body_on_200_is_surfaced(self, harness: Any) -> None:
        """Donetick answers 200 with an error body here; that must not read as success."""
        table = routes()
        table[("PUT", f"/api/v1/chores/{CHORE_ID}/start")] = ok(
            {"error": "Chore is not in a state that can be started"}
        )
        async with harness(table) as h:
            result = await h.client.call_tool("start_chore_timer", {"chore_id": CHORE_ID})
        assert "not in a state" in _payload(result)["error"]


class TestSubtaskTools:
    async def test_list_subtasks(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("list_subtasks", {"chore_id": CHORE_ID})
        data = _payload(result)
        assert [item["name"] for item in data] == ["front", "back"]
        assert data[0]["completed"] is False
        assert data[1]["completed"] is True

    async def test_create_subtask_appends(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_subtask", {"chore_id": CHORE_ID, "name": "side"}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert [t["name"] for t in body["subTasks"]] == ["front", "back", "side"]
        assert "added" in _payload(result)["message"]

    async def test_delete_subtask_by_name(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "delete_subtask", {"chore_id": CHORE_ID, "name": "back"}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert [t["name"] for t in body["subTasks"]] == ["front"]
        assert "deleted" in _payload(result)["message"]

    async def test_delete_subtask_by_id(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "delete_subtask", {"chore_id": CHORE_ID, "subtask_id": 2}
            )
        body = h.body_for("PUT", "/api/v1/chores/")
        assert [t["name"] for t in body["subTasks"]] == ["front"]

    async def test_delete_subtask_needs_a_selector(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("delete_subtask", {"chore_id": CHORE_ID})
        assert "either name or subtask_id" in _payload(result)["error"]

    async def test_delete_missing_subtask_is_reported(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "delete_subtask", {"chore_id": CHORE_ID, "name": "nope"}
            )
        assert "No subtask matching" in _payload(result)["error"]

    async def test_mark_complete(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "update_subtask_completion",
                {"chore_id": CHORE_ID, "subtask_id": 1, "completed": True},
            )
        body = h.body_for("PUT", f"/api/v1/chores/{CHORE_ID}/subtask")
        assert body["id"] == 1
        assert body["choreId"] == CHORE_ID
        assert body["completedAt"] is not None
        assert "completed" in _payload(result)["message"]

    async def test_reopen_sends_null(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "update_subtask_completion",
                {"chore_id": CHORE_ID, "subtask_id": 1, "completed": False},
            )
        body = h.body_for("PUT", f"/api/v1/chores/{CHORE_ID}/subtask")
        assert body["completedAt"] is None
        assert "reopened" in _payload(result)["message"]


class TestLabelTools:
    async def test_list_labels(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("list_labels", {})
        assert _payload(result) == [{"id": 1, "name": "outdoor", "color": "#0f0"}]

    async def test_create_label(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool(
                "create_label", {"name": "indoor", "color": "#123456"}
            )
        assert h.body_for("POST", "/api/v1/labels") == {
            "name": "indoor",
            "color": "#123456",
        }
        assert _payload(result)["name"] == "outdoor"

    async def test_update_label(self, harness: Any) -> None:
        table = routes()
        table[("PUT", "/api/v1/labels")] = ok(
            {"res": {"id": 1, "name": "outdoors", "color": "#0000ff"}}
        )
        async with harness(table) as h:
            result = await h.client.call_tool(
                "update_label",
                {"label_id": 1, "name": "outdoors", "color": "#0000ff"},
            )
        assert h.body_for("PUT", "/api/v1/labels") == {
            "id": 1,
            "name": "outdoors",
            "color": "#0000ff",
        }
        assert _payload(result)["name"] == "outdoors"

    async def test_delete_label(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("delete_label", {"label_id": 1})
        assert "DELETE /api/v1/labels/1" in h.paths()
        assert "deleted" in _payload(result)["message"]


class TestHistoryTools:
    async def test_chore_history(self, harness: Any) -> None:
        table = routes()
        table[("GET", f"/api/v1/chores/{CHORE_ID}/history")] = ok(
            {
                "res": [
                    {
                        "id": 1,
                        "choreId": CHORE_ID,
                        "performedAt": NOW,
                        "completedBy": 1,
                        "notes": "done",
                    }
                ]
            }
        )
        async with harness(table) as h:
            result = await h.client.call_tool(
                "get_chore_history", {"chore_id": CHORE_ID}
            )
        data = _payload(result)
        assert data[0]["chore_id"] == CHORE_ID
        assert data[0]["completed_by"] == 1

    async def test_circle_history(self, harness: Any) -> None:
        async with harness() as h:
            await h.client.call_tool(
                "get_history", {"duration_days": 14, "include_circle": True}
            )
        url = next(str(r.url) for r in h.requests if r.url.path.endswith("/history"))
        assert "limit=14" in url
        assert "includeCircle=true" in url


class TestUserTools:
    async def test_get_profile(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("get_profile", {})
        assert _payload(result)["username"] == "testuser"

    async def test_list_users_includes_role_and_points(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.call_tool("list_users", {})
        data = _payload(result)
        assert data[0]["id"] == 1
        assert data[0]["role"] == "owner"
        assert data[0]["points"] == 7


class TestResources:
    async def test_users_resource(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.read_resource("donetick://users")
        data = _payload(result)
        assert data[0]["username"] == "testuser"
        assert data[0]["role"] == "owner"

    async def test_labels_resource(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.read_resource("donetick://labels")
        assert _payload(result) == [{"id": 1, "name": "outdoor", "color": "#0f0"}]

    async def test_chore_resource(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.read_resource(f"donetick://chore/{CHORE_ID}")
        assert _payload(result)["name"] == "Mow lawn"

    async def test_chore_resource_reports_missing(self, harness: Any) -> None:
        table = routes()
        table[("GET", "/api/v1/chores/999")] = ok(
            {"error": "Failed to retrieve chore"}, 500
        )
        async with harness(table) as h:
            result = await h.client.read_resource("donetick://chore/999")
        assert "not found" in _payload(result)["error"]

    async def test_chores_resource(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.read_resource("donetick://chores")
        assert _payload(result)[0]["name"] == "Mow lawn"

    async def test_profile_resource(self, harness: Any) -> None:
        async with harness() as h:
            result = await h.client.read_resource("donetick://profile")
        assert _payload(result)["id"] == 1
