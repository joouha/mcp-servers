"""Donetick MCP Server.

An MCP server for managing household chores via the Donetick API.
"""

from __future__ import annotations

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Any

import httpx
import msgspec
from fastmcp import Context, FastMCP
from msgspec import UNSET, UnsetType

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Donetick API types
# ---------------------------------------------------------------------------


class FrequencyType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    ADAPTIVE = "adaptive"
    INTERVAL = "interval"
    DAYS_OF_THE_WEEK = "days_of_the_week"
    DAY_OF_THE_MONTH = "day_of_the_month"
    TRIGGER = "trigger"
    NO_REPEAT = "no_repeat"


class AssignmentStrategy(str, Enum):
    NO_ASSIGNEE = "no_assignee"
    RANDOM = "random"
    LEAST_ASSIGNED = "least_assigned"
    LEAST_COMPLETED = "least_completed"
    KEEP_LAST_ASSIGNED = "keep_last_assigned"
    RANDOM_EXCEPT_LAST_ASSIGNED = "random_except_last_assigned"
    ROUND_ROBIN = "round_robin"


class Status(int, Enum):
    NO_STATUS = 0
    IN_PROGRESS = 1
    PAUSED = 2


class FrequencyMetadata(msgspec.Struct, rename="camel", omit_defaults=True):
    days: list[str] | None = None
    months: list[str] | None = None
    unit: str | None = None
    time: datetime | None = None
    timezone: str = ""


class NotificationMetadata(msgspec.Struct, rename="camel", omit_defaults=True):
    due_date: bool | UnsetType = UNSET
    nagging: bool | UnsetType = UNSET
    predue: bool | UnsetType = UNSET
    completion: bool | UnsetType = UNSET
    circle_group: bool | UnsetType = UNSET
    circle_group_id: int | None = None


class Label(msgspec.Struct, rename="camel"):
    id: int
    name: str
    color: str
    created_by: int | UnsetType = UNSET


class SubTask(msgspec.Struct, rename="camel", omit_defaults=True):
    id: int
    order_id: int
    name: str
    completed_at: datetime | None = None
    completed_by: int | None = None
    parent_id: int | None = None


class ChoreAssignees(msgspec.Struct, rename="camel"):
    user_id: int


class DonetickChore(msgspec.Struct, rename="camel", omit_defaults=True, kw_only=True):
    id: int | None = None
    name: str
    frequency: int = 0
    frequency_type: FrequencyType = FrequencyType.ONCE
    frequency_metadata: FrequencyMetadata | None = None
    next_due_date: datetime | None = None
    is_rolling: bool = False
    assigned_to: int | None = None
    assignees: list[ChoreAssignees] = msgspec.field(default_factory=list)
    assign_strategy: AssignmentStrategy = AssignmentStrategy.ROUND_ROBIN
    is_active: bool = True
    notification: bool = True
    notification_metadata: NotificationMetadata | None = msgspec.field(
        default_factory=NotificationMetadata
    )
    labels: str | None = None
    labels_v2: list[Label] | None = None
    circle_id: int = 0
    created_at: datetime | UnsetType = UNSET
    updated_at: datetime | UnsetType = UNSET
    created_by: int | UnsetType = UNSET
    updated_by: int | UnsetType = UNSET
    status: Status = Status.NO_STATUS
    priority: int = 0
    completion_window: int | None = None
    points: int | None = None
    description: str | None = None
    sub_tasks: list[SubTask] | None = None


class ChoreReq(msgspec.Struct, rename="camel"):
    name: str
    assign_strategy: AssignmentStrategy = AssignmentStrategy.ROUND_ROBIN
    frequency_type: FrequencyType = FrequencyType.ONCE
    id: int | None = None
    due_date: str = ""
    assignees: list[ChoreAssignees] = msgspec.field(default_factory=list)
    assigned_to: int = 0
    is_rolling: bool = False
    is_active: bool = True
    frequency: int = 0
    frequency_metadata: FrequencyMetadata | None = None
    notification_metadata: NotificationMetadata | None = None
    labels_v2: list[Label] | None = None
    points: int | None = None
    completion_window: int | None = None
    description: str | None = None
    priority: int = 0
    sub_tasks: list[SubTask] | None = None


class AuthResp(msgspec.Struct, rename="camel"):
    code: int = 0
    message: str = ""
    token: str | UnsetType = UNSET
    expire: datetime | UnsetType = UNSET


class UserProfile(msgspec.Struct, rename="camel"):
    id: int
    display_name: str = ""
    email: str = ""
    username: str = ""
    circle_id: int = 0


# ---------------------------------------------------------------------------
# Donetick HTTP client
# ---------------------------------------------------------------------------


class DonetickClient:
    """Authenticated client for the Donetick REST API."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        timeout: int = 10,
    ) -> None:
        self.url = httpx.URL(url)
        self._username = username
        self._password = password
        self._client = httpx.Client(timeout=timeout)
        self._token: str = ""
        self._token_expire: datetime | None = None

    # -- auth ---------------------------------------------------------------

    def _ensure_auth(self) -> None:
        from datetime import UTC, datetime as dt

        if self._token and self._token_expire and self._token_expire > dt.now(UTC):
            return
        resp = self._client.post(
            self.url.join("./api/v1/auth/login"),
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        raw = resp.json()
        log.debug("Auth response: %s", raw)

        # Some Donetick instances return {token, expire} at top level,
        # others wrap in {code, message, token, expire}.
        token = raw.get("token")
        expire = raw.get("expire")

        if not token:
            code = raw.get("code", 0)
            message = raw.get("message", "unknown error")
            msg = f"Donetick auth failed (code={code}): {message}"
            raise RuntimeError(msg)

        self._token = token
        if isinstance(expire, str):
            # Handle nanosecond-precision timestamps by truncating to microseconds
            try:
                self._token_expire = datetime.fromisoformat(expire)
            except ValueError:
                # Strip trailing nanosecond digits beyond microsecond precision
                import re

                truncated = re.sub(r"(\.\d{6})\d+", r"\1", expire)
                self._token_expire = datetime.fromisoformat(truncated)
        else:
            self._token_expire = None
        self._client.headers["Authorization"] = f"Bearer {self._token}"

    # -- helpers ------------------------------------------------------------

    def _get(self, path: str) -> Any:
        self._ensure_auth()
        resp = self._client.get(self.url.join(path))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: Any = None) -> Any:
        self._ensure_auth()
        resp = self._client.post(self.url.join(path), json=json)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json: Any = None) -> Any:
        self._ensure_auth()
        resp = self._client.put(self.url.join(path), json=json)
        resp.raise_for_status()
        return resp.json()

    # -- public API ---------------------------------------------------------

    def get_profile(self) -> UserProfile:
        data = self._get("./api/v1/users/profile")
        return msgspec.convert(data.get("res", {}), type=UserProfile)

    def get_users(self) -> list[dict[str, Any]]:
        data = self._get("./api/v1/users/")
        return data.get("res", [])

    def list_chores(self) -> list[DonetickChore]:
        data = self._get("./api/v1/chores/")
        return msgspec.convert(data.get("res", []), type=list[DonetickChore])

    def get_chore(self, chore_id: int) -> DonetickChore | None:
        data = self._get(f"./api/v1/chores/{chore_id}")
        if "error" in data:
            return None
        return msgspec.convert(data.get("res", {}), type=DonetickChore)

    def create_chore(self, req: ChoreReq) -> int:
        req_json = msgspec.to_builtins(req)
        data = self._post("./api/v1/chores/", json=req_json)
        return data.get("res")

    def update_chore(self, req: ChoreReq) -> None:
        req_json = msgspec.to_builtins(req)
        self._put("./api/v1/chores/", json=req_json)

    def complete_chore(self, chore_id: int, note: str | None = None) -> DonetickChore:
        data = self._post(f"./api/v1/chores/{chore_id}/do", json={"note": note})
        return msgspec.convert(data.get("res", {}), type=DonetickChore)

    def archive_chore(self, chore_id: int) -> None:
        self._put(f"./api/v1/chores/{chore_id}/archive")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chore_summary(chore: DonetickChore) -> dict[str, Any]:
    """Return a compact summary dict for list/search results."""
    return {
        "id": chore.id,
        "name": chore.name,
        "due": chore.next_due_date.isoformat() if chore.next_due_date else None,
        "active": chore.is_active,
        "assigned_to": chore.assigned_to or 0,
        "frequency_type": chore.frequency_type.value,
        "priority": chore.priority,
    }


def _chore_detail(chore: DonetickChore) -> dict[str, Any]:
    """Return a full detail dict for a single chore."""
    return {
        "id": chore.id,
        "name": chore.name,
        "description": chore.description,
        "due": chore.next_due_date.isoformat() if chore.next_due_date else None,
        "active": chore.is_active,
        "assigned_to": chore.assigned_to or 0,
        "assignees": [a.user_id for a in chore.assignees],
        "assign_strategy": chore.assign_strategy.value,
        "frequency": chore.frequency,
        "frequency_type": chore.frequency_type.value,
        "frequency_metadata": msgspec.to_builtins(chore.frequency_metadata)
        if chore.frequency_metadata
        else None,
        "is_rolling": chore.is_rolling,
        "priority": chore.priority,
        "points": chore.points,
        "status": chore.status.value,
        "labels": [l.name for l in chore.labels_v2] if chore.labels_v2 else [],
        "sub_tasks": [
            {"id": s.id, "name": s.name, "completed": s.completed_at is not None}
            for s in chore.sub_tasks
        ]
        if chore.sub_tasks
        else [],
        "created_at": chore.created_at.isoformat()
        if not isinstance(chore.created_at, UnsetType)
        else None,
        "updated_at": chore.updated_at.isoformat()
        if not isinstance(chore.updated_at, UnsetType)
        else None,
    }


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create a single DonetickClient for the server's lifetime."""
    url = os.environ.get("DONETICK_URL", "https://donetick.com/")
    username = os.environ.get("DONETICK_USERNAME", "")
    password = os.environ.get("DONETICK_PASSWORD", "")
    timeout = int(os.environ.get("DONETICK_TIMEOUT", "10"))
    if not username or not password:
        msg = (
            "DONETICK_USERNAME and DONETICK_PASSWORD environment variables are required"
        )
        raise RuntimeError(msg)
    client = DonetickClient(
        url=url, username=username, password=password, timeout=timeout
    )
    try:
        yield {"client": client}
    finally:
        client._client.close()


mcp = FastMCP(
    "Donetick",
    instructions=(
        "MCP server for managing household chores via the Donetick API. "
        "Use the provided tools to list, search, create, update, complete, "
        "and delete chores."
    ),
    lifespan=lifespan,
)


def _get_client(ctx: Context) -> DonetickClient:
    """Retrieve the shared DonetickClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


@mcp.tool()
def list_chores(ctx: Context) -> list[dict[str, Any]]:
    """List all chores with summary info (id, name, due date, status)."""
    client = _get_client(ctx)
    chores = client.list_chores()
    return [_chore_summary(c) for c in chores]


@mcp.tool()
def search_chores(ctx: Context, query: str) -> list[dict[str, Any]]:
    """Search chores by name or description (case-insensitive substring match).

    Args:
        query: Text to search for in chore names and descriptions.
    """
    client = _get_client(ctx)
    chores = client.list_chores()
    q = query.lower()
    results = [
        c
        for c in chores
        if q in c.name.lower() or (c.description and q in c.description.lower())
    ]
    return [_chore_summary(c) for c in results]


@mcp.tool()
def get_chore(ctx: Context, chore_id: int) -> dict[str, Any]:
    """Get full details of a single chore.

    Args:
        chore_id: The ID of the chore to retrieve.
    """
    client = _get_client(ctx)
    chore = client.get_chore(chore_id)
    if chore is None:
        return {"error": f"Chore {chore_id} not found"}
    return _chore_detail(chore)


@mcp.tool()
def create_chore(
    ctx: Context,
    name: str,
    description: str | None = None,
    due_date: str | None = None,
    assigned_to: int = 0,
    assignees: list[int] | None = None,
    assign_strategy: str = "round_robin",
    frequency_type: str = "once",
    frequency: int = 0,
    is_rolling: bool = False,
    priority: int = 0,
    points: int | None = None,
) -> dict[str, Any]:
    """Create a new chore.

    Args:
        name: Name of the chore.
        description: Optional description.
        due_date: Due date as ISO 8601 string (e.g. "2025-01-15T10:00:00Z").
        assigned_to: User ID to assign the chore to.
        assignees: List of user IDs who can be assigned.
        assign_strategy: Assignment strategy (random, least_assigned, least_completed, keep_last_assigned, random_except_last_assigned, round_robin).
        frequency_type: Recurrence type (once, daily, weekly, monthly, yearly, interval, days_of_the_week, day_of_the_month, no_repeat).
        frequency: Frequency interval value.
        is_rolling: Whether the due date rolls from completion date.
        priority: Priority level (0 = none).
        points: Optional point value for the chore.
    """
    client = _get_client(ctx)
    req = ChoreReq(
        name=name,
        description=description,
        due_date=due_date or "",
        assigned_to=assigned_to,
        assignees=[ChoreAssignees(user_id=uid) for uid in (assignees or [])],
        assign_strategy=AssignmentStrategy(assign_strategy),
        frequency_type=FrequencyType(frequency_type),
        frequency=frequency,
        is_rolling=is_rolling,
        priority=priority,
        points=points,
    )
    chore_id = client.create_chore(req)
    return {"id": chore_id, "message": f"Chore '{name}' created successfully"}


@mcp.tool()
def update_chore(
    ctx: Context,
    chore_id: int,
    name: str | None = None,
    description: str | None = None,
    due_date: str | None = None,
    assigned_to: int | None = None,
    assignees: list[int] | None = None,
    assign_strategy: str | None = None,
    frequency_type: str | None = None,
    frequency: int | None = None,
    is_rolling: bool | None = None,
    priority: int | None = None,
    points: int | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    """Update an existing chore. Only provided fields are changed.

    Args:
        chore_id: The ID of the chore to update.
        name: New name for the chore.
        description: New description.
        due_date: New due date as ISO 8601 string.
        assigned_to: New assigned user ID.
        assignees: New list of assignee user IDs.
        assign_strategy: New assignment strategy.
        frequency_type: New recurrence type.
        frequency: New frequency interval.
        is_rolling: Whether the due date rolls from completion date.
        priority: New priority level.
        points: New point value.
        is_active: Whether the chore is active.
    """
    client = _get_client(ctx)
    existing = client.get_chore(chore_id)
    if existing is None:
        return {"error": f"Chore {chore_id} not found"}

    # Build the request from the existing chore, overriding supplied fields
    req = msgspec.convert(existing, type=ChoreReq, from_attributes=True)

    if name is not None:
        req.name = name
    if description is not None:
        req.description = description
    if due_date is not None:
        req.due_date = due_date
    if assigned_to is not None:
        req.assigned_to = assigned_to
    if assignees is not None:
        req.assignees = [ChoreAssignees(user_id=uid) for uid in assignees]
    if assign_strategy is not None:
        req.assign_strategy = AssignmentStrategy(assign_strategy)
    if frequency_type is not None:
        req.frequency_type = FrequencyType(frequency_type)
    if frequency is not None:
        req.frequency = frequency
    if is_rolling is not None:
        req.is_rolling = is_rolling
    if priority is not None:
        req.priority = priority
    if points is not None:
        req.points = points
    if is_active is not None:
        req.is_active = is_active

    client.update_chore(req)
    return {"message": f"Chore {chore_id} updated successfully"}


@mcp.tool()
def complete_chore(
    ctx: Context, chore_id: int, note: str | None = None
) -> dict[str, Any]:
    """Mark a chore as done. Recurring chores will auto-reschedule.

    Args:
        chore_id: The ID of the chore to complete.
        note: Optional note to attach to the completion.
    """
    client = _get_client(ctx)
    updated = client.complete_chore(chore_id, note=note)
    result = {
        "message": f"Chore {chore_id} marked as complete",
        "chore": _chore_summary(updated),
    }
    if updated.is_active and updated.next_due_date:
        result["rescheduled"] = True
        result["next_due"] = updated.next_due_date.isoformat()
    return result


@mcp.tool()
def delete_chore(ctx: Context, chore_id: int) -> dict[str, Any]:
    """Archive (delete) a chore.

    Args:
        chore_id: The ID of the chore to archive.
    """
    client = _get_client(ctx)
    client.archive_chore(chore_id)
    return {"message": f"Chore {chore_id} archived successfully"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Donetick MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
