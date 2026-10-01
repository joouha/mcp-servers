"""Async HTTP client for the Donetick REST API.

Design notes
------------
* Fully async (``httpx.AsyncClient``) so a slow Donetick instance never blocks
  the MCP server's event loop.
* Token-bucket rate limiting keeps the client inside Donetick's
  ``DT_SERVER_RATE_LIMIT`` window without bursting.
* Transient failures are retried with exponential backoff plus jitter, and
  ``429`` responses honour ``Retry-After``.
* A ``401`` triggers a re-authentication (refresh token first, then a full
  login) and the request is replayed exactly once.
* Donetick sometimes answers ``200 OK`` with an ``error`` key in the body
  (e.g. ``PUT /chores/{id}/start`` when the chore is not startable).  Those
  responses are converted into exceptions too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime
from types import TracebackType
from typing import Any

import httpx

from .config import Config
from .models import (
    ChoreHistoryEntry,
    ChoreReq,
    CircleMember,
    DonetickChore,
    FrequencyType,
    Label,
    SubTask,
    TimerState,
    UserProfile,
)
from .transforms import chore_status_label, labels_for_update

log = logging.getLogger(__name__)

#: Status codes worth retrying.  ``500`` is deliberately excluded: Donetick uses
#: it to report application errors such as "Failed to retrieve chore", which
#: will never succeed on a replay.
RETRY_STATUS = frozenset({408, 425, 429, 502, 503, 504})

#: Chore-not-found conditions.  Donetick answers a missing chore with ``500``
#: and ``{"error": "Failed to retrieve chore"}``, which is unfortunate but
#: consistent enough to detect alongside a plain ``404``.
_NOT_FOUND_MARKERS = ("failed to retrieve chore", "chore not found", "record not found")


def _field(body: Any, name: str) -> Any:
    """Read ``name`` from a JSON object, tolerating non-dict bodies."""
    if isinstance(body, dict):
        return body.get(name)
    return None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DonetickError(RuntimeError):
    """Base class for every error raised by this client."""


class DonetickAuthError(DonetickError):
    """Raised when authentication fails or the account requires MFA."""


class DonetickNotFoundError(DonetickError):
    """Raised when a requested resource does not exist."""


class DonetickTransportError(DonetickError):
    """Raised when Donetick closes a connection mid-response."""


# Backwards-compatible alias for the previous private name.
_DonetickTransportError = DonetickTransportError


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TokenBucket:
    """A simple async-safe token bucket.

    Args:
        rate: Tokens added per second.
        burst: Maximum tokens held at once.
    """

    __slots__ = ("_capacity", "_lock", "_rate", "_tokens", "_updated")

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._rate,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DonetickClient:
    """Authenticated, rate-limited client for the Donetick REST API.

    Use :meth:`aclose` (or the async context manager protocol) when finished so
    the underlying connection pool is released.
    """

    def __init__(
        self,
        config: Config,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.url = config.url
        self._base = config.url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout,
            verify=config.verify_tls,
            follow_redirects=False,
        )
        self._bucket = TokenBucket(config.rate_limit_per_second, config.rate_limit_burst)
        self._auth_lock = asyncio.Lock()
        self._token = ""
        self._token_expire: float = 0.0
        self._refresh_token = ""

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> DonetickClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- authentication -----------------------------------------------------

    @property
    def _expired(self) -> bool:
        return not self._token or time.time() >= self._token_expire

    async def ensure_auth(self, *, force: bool = False) -> None:
        """Authenticate if needed, refreshing or logging in as required.

        Args:
            force: Re-authenticate even when the current token looks valid.
        """
        async with self._auth_lock:
            if not force and not self._expired:
                return
            # Prefer the refresh token: it avoids a full login round-trip.
            if self._refresh_token and not (
                force and self._token and not self._expired
            ):
                try:
                    await self._refresh_locked()
                    return
                except DonetickAuthError:
                    log.debug("Refresh token rejected; falling back to login")
                    self._refresh_token = ""
            await self._login_locked()

    async def _login_locked(self) -> None:
        response = await self._client.post(
            f"{self._base}/api/v1/auth/login",
            json={"username": self.config.username, "password": self.config.password},
        )
        body = self._json(response)
        if response.status_code != httpx.codes.OK:
            raise DonetickAuthError(self._error_message(response, body, "login failed"))

        if body.get("mfaRequired") or body.get("mfa_required"):
            msg = (
                "Donetick requires multi-factor authentication for this account; "
                "use a dedicated API/automation user instead"
            )
            raise DonetickAuthError(msg)

        self._store_tokens(body)

    async def _refresh_locked(self) -> None:
        response = await self._client.post(
            f"{self._base}/api/v1/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        body = self._json(response)
        if response.status_code != httpx.codes.OK:
            raise DonetickAuthError(
                self._error_message(response, body, "token refresh failed")
            )
        self._store_tokens(body)

    def _store_tokens(self, body: Any) -> None:
        """Record the token pair from a login or refresh response."""
        # Donetick returns `access_token` alongside a legacy `token` mirror;
        # older releases only populate `token`.
        token = _field(body, "access_token") or _field(body, "token")
        if not token:
            msg = "Donetick auth response did not contain a token"
            raise DonetickAuthError(msg)

        self._token = str(token)
        self._refresh_token = str(_field(body, "refresh_token") or "")
        expires = _field(body, "access_token_expiry") or _field(body, "expire")
        self._token_expire = self._parse_expiry(expires)
        self._client.headers["Authorization"] = f"Bearer {self._token}"

    @staticmethod
    def _parse_expiry(value: Any) -> float:
        """Turn an ISO expiry timestamp into a POSIX timestamp.

        Donetick emits microsecond precision, but tolerate nanoseconds too.
        Unparseable values fall back to a short session so the client
        re-authenticates rather than trusting an unknown lifetime.
        """
        if not isinstance(value, str) or not value:
            return time.time() + 300
        from datetime import datetime

        normalised = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            import re

            truncated = re.sub(r"(\.\d{6})\d+", r"\1", normalised)
            try:
                parsed = datetime.fromisoformat(truncated)
            except ValueError:
                return time.time() + 300
        if parsed.tzinfo is None:
            return parsed.timestamp()
        return parsed.timestamp()

    # -- transport ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base}/{path.lstrip('/')}"

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        """Decode a response body, returning ``{}`` when it is not valid JSON."""
        with contextlib.suppress(ValueError):
            return response.json()
        return {}

    @staticmethod
    def _error_message(
        response: httpx.Response, body: Any = None, prefix: str = ""
    ) -> str:
        """Build a human-readable message from Donetick's error payload.

        Donetick puts the useful text in ``error`` and, for binding failures,
        a verbose ``details`` blob that names the offending field.
        """
        body = body if isinstance(body, dict) else {}
        detail = _field(body, "error") or _field(body, "message") or ""
        details = _field(body, "details") or ""
        if not detail:
            detail = details
        if not detail:
            detail = response.text.strip()[:300] or response.reason_phrase
        parts = [f"Donetick {response.status_code} for {response.request.url}"]
        if prefix:
            parts.insert(0, f"{prefix}")
        message = ": ".join(parts) + f": {detail}"
        if details and details != detail:
            message = f"{message} ({details})"
        return message

    @staticmethod
    def _raise_body_error(response: httpx.Response, body: Any) -> None:
        """Turn a 200 response whose body carries an ``error`` into an exception.

        Several Donetick handlers report failures with a 200 status and an
        ``error`` key -- starting the timer on an unstartable chore, or
        deleting a chore that no longer exists, both do this.  Treating them
        as success would report a lie back to the caller.
        """
        if isinstance(body, dict) and _field(body, "error") and "res" not in body:
            raise DonetickError(DonetickClient._error_message(response, body))

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        allow_remote_disconnect: bool = False,
        authenticated: bool = True,
    ) -> Any:
        """Perform a request with rate limiting, retries and re-auth.

        Args:
            method: HTTP verb.
            path: API path, e.g. ``/api/v1/chores/``.
            json: Optional JSON body.
            allow_remote_disconnect: Raise :class:`DonetickTransportError`
                instead of propagating ``httpx.RemoteProtocolError`` so callers
                can attempt recovery.
            authenticated: Whether to attach the bearer token.

        Raises:
            DonetickAuthError: If authentication fails.
            DonetickNotFoundError: If the resource does not exist.
            DonetickTransportError: On a mid-response disconnect when allowed.
            DonetickError: For any other API-level failure.

        Returns:
            The decoded JSON body (``None`` when the body is empty).
        """
        if authenticated:
            await self.ensure_auth()

        url = self._url(path)
        attempts = self.config.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            await self._bucket.acquire()
            try:
                response = await self._client.request(method, url, json=json)
            except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                if allow_remote_disconnect:
                    msg = f"Donetick disconnected while handling {method} {path}"
                    raise DonetickTransportError(msg) from exc
                last_error = exc
                if attempt + 1 >= attempts:
                    msg = f"Donetick request to {path} failed: {exc}"
                    raise DonetickError(msg) from exc
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == httpx.codes.UNAUTHORIZED and authenticated:
                # The token was revoked or rotated. Re-authenticate and replay;
                # the attempt budget bounds this so a permanently rejected
                # account cannot loop forever.
                await self.ensure_auth(force=True)
                last_error = DonetickAuthError(
                    self._error_message(response, self._json(response), "rejected")
                )
                continue

            if response.status_code in RETRY_STATUS and attempt + 1 < attempts:
                delay = self._retry_after(response) or self._backoff(attempt)
                log.warning(
                    "Donetick %s on %s %s; retrying in %.2fs (attempt %d/%d)",
                    response.status_code,
                    method,
                    path,
                    delay,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code == httpx.codes.NOT_FOUND:
                raise DonetickNotFoundError(
                    self._error_message(response, self._json(response), "not found")
                )

            if not response.is_success:
                raise DonetickError(self._error_message(response))

            body = self._json(response)
            self._raise_body_error(response, body)
            return body

        if last_error is not None:
            raise DonetickError(str(last_error)) from last_error
        msg = f"Donetick request to {path} exhausted all retries"
        raise DonetickError(msg)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 10 seconds."""
        return random.uniform(0, min(10.0, 0.5 * 2**attempt))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(30.0, float(raw))
        except ValueError:
            return None

    async def _sleep_backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff(attempt))

    async def _get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def _post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json=json, **kwargs)

    async def _put(self, path: str, json: Any = None) -> Any:
        return await self.request("PUT", path, json=json)

    async def _delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    # -- users --------------------------------------------------------------

    async def get_profile(self) -> UserProfile:
        """Return the authenticated user's profile."""
        body = await self._get("/api/v1/users/profile")
        return UserProfile.model_validate(body.get("res") or {})

    async def get_users(self) -> list[dict[str, Any]]:
        """Return circle users from the legacy ``/users/`` endpoint."""
        body = await self._get("/api/v1/users/")
        return list(body.get("res") or [])

    async def get_circle_members(self) -> list[CircleMember]:
        """Return circle members with roles and points.

        ``/circles/members`` has no trailing slash; adding one triggers a 301
        which this client does not follow.
        """
        body = await self._get("/api/v1/circles/members")
        return [CircleMember.model_validate(m) for m in body.get("res") or []]

    async def get_label_catalog(self) -> dict[str, int]:
        """Return a mapping of lowercased label name to label ID."""
        labels = await self.list_labels()
        return {label.name.lower(): label.id for label in labels}

    # -- chores -------------------------------------------------------------

    async def list_chores(self) -> list[DonetickChore]:
        """Return every active chore in the circle."""
        body = await self._get("/api/v1/chores/")
        return [DonetickChore.model_validate(c) for c in body.get("res") or []]

    async def list_archived_chores(self) -> list[DonetickChore]:
        """Return archived chores."""
        body = await self._get("/api/v1/chores/archived")
        return [DonetickChore.model_validate(c) for c in body.get("res") or []]

    async def get_chore(self, chore_id: int) -> DonetickChore | None:
        """Return one chore, or ``None`` when it does not exist."""
        try:
            body = await self._get(f"/api/v1/chores/{chore_id}")
        except DonetickNotFoundError:
            return None
        except DonetickError as exc:
            if self._is_missing_chore(exc):
                return None
            raise
        res = body.get("res")
        if not res:
            return None
        return DonetickChore.model_validate(res)

    @staticmethod
    def _is_missing_chore(exc: Exception) -> bool:
        """Detect Donetick's idiosyncratic not-found responses."""
        message = str(exc).lower()
        return any(marker in message for marker in _NOT_FOUND_MARKERS)

    async def create_chore(self, req: ChoreReq) -> int:
        """Create a chore and return its ID.

        Donetick occasionally drops the connection after committing a write.
        When that happens, look for the chore that was just created instead of
        failing the whole call.

        Raises:
            DonetickError: If the chore cannot be found after a disconnect.
        """
        payload = req.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            body = await self._post(
                "/api/v1/chores/",
                json=payload,
                allow_remote_disconnect=req.next_due_date is not None,
            )
        except DonetickTransportError:
            chore_id = await self._recover_created_chore(req)
            if chore_id is not None:
                log.warning("Recovered chore creation after disconnect: %s", chore_id)
                return chore_id
            raise

        chore_id = body.get("res")
        if not isinstance(chore_id, int):
            msg = "Donetick create_chore response did not include a valid chore id"
            raise DonetickError(msg)
        return chore_id

    async def _recover_created_chore(self, req: ChoreReq) -> int | None:
        """Find the chore matching a create that appeared to fail."""
        candidates = [
            chore
            for chore in await self.list_chores()
            if chore.name == req.name and chore.next_due_date is not None
        ]
        if req.next_due_date is not None:
            target = req.next_due_date.timestamp()
            exact = [
                chore
                for chore in candidates
                if chore.next_due_date is not None
                and abs(chore.next_due_date.timestamp() - target) < 1
            ]
            candidates = exact or candidates
        if req.assigned_to is not None:
            narrowed = [c for c in candidates if c.assigned_to == req.assigned_to]
            candidates = narrowed or candidates
        if not candidates:
            return None
        newest = max(candidates, key=lambda c: c.id if c.id is not None else -1)
        return newest.id

    async def update_chore(self, req: ChoreReq) -> list[str]:
        """Update a chore from a full replacement payload.

        Returns:
            Any warnings Donetick attached to the response.
        """
        # `updatedAt` is Donetick's optimistic lock. Its `CanEdit` check
        # rejects the write if the chore changed after that timestamp -- and
        # because the server stamps `UpdatedAt` slightly ahead of what a
        # client can read, echoing back the value from a fresh GET still 403s.
        # Omitting it skips the conflict check, which is what a
        # read-modify-write caller wants, so `ChoreReq` has no such field.
        #
        # `syncVersion` is not part of `ChoreReq` at all and is dropped for
        # good measure, since it is server-managed.
        # `exclude_none` must stay off here.  This endpoint is a full
        # replacement, so a key left out of the body is reset server-side:
        # dropping the nulls silently blanked every unset field on every
        # edit -- most visibly `nextDueDate`, which is how chore due dates
        # went missing.  Dumping the nulls keeps an explicitly-unset field
        # (an unassigned chore, a chore with no points) unchanged.
        payload = req.model_dump(mode="json", by_alias=True)
        payload.pop("syncVersion", None)
        # Donetick's edit handler dereferences *choreReq.LabelsV2 and
        # *choreReq.SubTasks with no nil guard, unlike its create handler.
        # Omitting either key -- or sending an explicit null -- panics the
        # server, which drops the connection without any HTTP response, so
        # both are always sent, empty when there is nothing to carry.
        payload["labelsV2"] = payload.get("labelsV2") or []
        payload["subTasks"] = payload.get("subTasks") or []
        body = await self._put("/api/v1/chores/", json=payload)
        return list(body.get("warnings") or [])

    async def update_due_date(self, chore_id: int, due_date: datetime) -> None:
        """Move a chore's due date without rewriting the rest of the record.

        There is no standalone due-date route any more.  The former
        ``PUT /api/v1/chores/{id}/dueDate`` sent a ``dueDate`` field that the
        shared create/update struct no longer binds, and the path itself is no
        longer served -- it answers 403.  The due date lives in ``nextDueDate``
        on the main PUT, so this is a read-modify-write that carries every
        other field across unchanged.
        """
        chore = await self.get_chore(chore_id)
        if chore is None:
            msg = f"Chore {chore_id} not found"
            raise DonetickNotFoundError(msg)
        await self.update_chore(await self.chore_to_req(chore, next_due_date=due_date))

    async def complete_chore(self, chore_id: int, note: str | None = None) -> DonetickChore:
        """Mark a chore done; recurring chores reschedule themselves."""
        body = await self._post(
            f"/api/v1/chores/{chore_id}/do", json={"note": note, "notes": note}
        )
        return DonetickChore.model_validate(body.get("res") or {})

    async def skip_chore(self, chore_id: int, note: str | None = None) -> None:
        """Skip the current occurrence of a chore."""
        await self._post(f"/api/v1/chores/{chore_id}/skip", json={"notes": note})

    async def approve_chore(self, chore_id: int) -> None:
        """Approve a chore that was completed with ``requireApproval`` set."""
        await self._post(f"/api/v1/chores/{chore_id}/approve")

    async def reject_chore(self, chore_id: int, reason: str | None = None) -> None:
        """Reject a chore completion, returning it to the assignee."""
        await self._post(f"/api/v1/chores/{chore_id}/reject", json={"notes": reason})

    async def archive_chore(self, chore_id: int) -> None:
        """Archive (soft-delete) a chore."""
        await self._put(f"/api/v1/chores/{chore_id}/archive")

    async def unarchive_chore(self, chore_id: int) -> None:
        """Restore an archived chore."""
        await self._put(f"/api/v1/chores/{chore_id}/unarchive")

    async def delete_chore(self, chore_id: int) -> None:
        """Permanently delete a chore and its history."""
        await self._delete(f"/api/v1/chores/{chore_id}")

    # -- timers -------------------------------------------------------------

    async def start_timer(self, chore_id: int) -> TimerState:
        """Start the chore timer.

        Raises:
            DonetickError: If the timer is already running.  Donetick reports
                this as ``200 {"error": "Chore is not in a state that can be
                started"}`` rather than an error status.
        """
        return await self._timer(f"/api/v1/chores/{chore_id}/start", chore_id)

    async def pause_timer(self, chore_id: int) -> TimerState:
        """Pause the chore timer."""
        return await self._timer(f"/api/v1/chores/{chore_id}/pause", chore_id)

    async def _timer(self, path: str, chore_id: int) -> TimerState:
        body = await self._put(path)
        res = dict(body.get("res") or {})
        # The endpoint reports the chore's status under the name `status`.
        res["chore_status"] = res.pop("status", 0)
        state = TimerState.model_validate({**res, "chore_id": chore_id})
        state.chore_status_label = chore_status_label(state.chore_status)
        return state

    # -- subtasks -----------------------------------------------------------

    async def set_subtask_completion(
        self, chore_id: int, subtask_id: int, completed_at: str | None
    ) -> None:
        """Mark a subtask complete, or clear it by passing ``None``."""
        await self._put(
            f"/api/v1/chores/{chore_id}/subtask",
            json={"id": subtask_id, "choreId": chore_id, "completedAt": completed_at},
        )

    async def replace_subtasks(
        self, chore: DonetickChore, subtasks: list[SubTask]
    ) -> list[str]:
        """Rewrite a chore's subtask list, returning any warnings.

        Donetick has no dedicated create/delete endpoint for subtasks; the list
        sent with a chore update is authoritative.
        """
        req = await self.chore_to_req(chore)
        req.sub_tasks = subtasks
        return await self.update_chore(req)

    # -- labels -------------------------------------------------------------

    async def list_labels(self) -> list[Label]:
        """Return the circle's labels.

        ``GET /api/v1/labels`` returns a bare JSON array rather than the usual
        ``{"res": ...}`` envelope, and rejects a trailing slash.
        """
        response = await self.request("GET", "/api/v1/labels")
        if isinstance(response, dict):
            response = response.get("res") or []
        return [Label.model_validate(item) for item in response or []]

    async def create_label(self, name: str, color: str) -> Label:
        """Create a label and return it."""
        body = await self._post("/api/v1/labels", json={"name": name, "color": color})
        return Label.model_validate(body.get("res") or {})

    async def update_label(self, label_id: int, name: str, color: str) -> Label:
        """Rename or recolour a label."""
        body = await self._put(
            "/api/v1/labels", json={"id": label_id, "name": name, "color": color}
        )
        return Label.model_validate(body.get("res") or {})

    async def delete_label(self, label_id: int) -> None:
        """Delete a label."""
        await self._delete(f"/api/v1/labels/{label_id}")

    # -- history ------------------------------------------------------------

    async def get_chore_history(
        self, chore_id: int, duration_days: int = 30
    ) -> list[ChoreHistoryEntry]:
        """Return a chore's completion history."""
        body = await self._get(
            f"/api/v1/chores/{chore_id}/history?duration={duration_days}"
        )
        return [ChoreHistoryEntry.model_validate(e) for e in body.get("res") or []]

    async def get_history(
        self,
        duration_days: int = 7,
        include_circle: bool = False,
    ) -> list[ChoreHistoryEntry]:
        """Return recent completion history across the circle."""
        query = f"limit={duration_days}&includeCircle={str(include_circle).lower()}"
        body = await self._get(f"/api/v1/chores/history?{query}")
        return [ChoreHistoryEntry.model_validate(e) for e in body.get("res") or []]

    # -- helpers ------------------------------------------------------------

    async def chore_to_req(
        self,
        chore: DonetickChore,
        *,
        next_due_date: datetime | None = None,
    ) -> ChoreReq:
        """Project a fetched chore into a full update payload.

        Donetick's update endpoint is a full replacement, so callers start
        from the current state and layer their changes on top.

        Args:
            chore: The chore as currently stored.
            next_due_date: Overrides the stored due date.  The override is
                applied *before* the payload is validated rather than
                assigned onto it afterwards, because ``ChoreReq`` rejects a
                rolling chore that has no due date.  Without the override a
                chore that has already lost its date could not be edited at
                all -- least of all have that date restored.
        """
        due = next_due_date if next_due_date is not None else chore.next_due_date
        return ChoreReq(
            id=chore.id,
            name=chore.name,
            frequency_type=chore.frequency_type,
            frequency=chore.frequency or 1,
            frequency_metadata=chore.frequency_metadata,
            next_due_date=due,
            is_rolling=chore.is_rolling,
            assignees=list(chore.assignees),
            assigned_to=chore.assigned_to,
            assign_strategy=chore.assign_strategy,
            is_active=chore.is_active,
            notification=chore.notification,
            notification_metadata=chore.notification_metadata,
            labels_v2=labels_for_update(chore),
            priority=chore.priority,
            completion_window=chore.completion_window,
            points=chore.points,
            description=chore.description or "",
            sub_tasks=list(chore.sub_tasks or []),
            require_approval=chore.require_approval,
            is_private=chore.is_private,
            project_id=chore.project_id,
        )

    @staticmethod
    def is_recurring(frequency_type: FrequencyType) -> bool:
        """Whether completing a chore of this type schedules another one."""
        return frequency_type not in (
            FrequencyType.ONCE,
            FrequencyType.NO_REPEAT,
            FrequencyType.TRIGGER,
        )
