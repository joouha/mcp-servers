"""FastMCP tools and resources exposing the Donetick API.

Every tool is async so a slow Donetick instance cannot stall the event loop,
and every tool returns either a structured model or an ``ErrorResponse``
rather than raising, which keeps failures legible for the calling agent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastmcp import Context, FastMCP

from .client import DonetickClient, DonetickError, DonetickNotFoundError
from .config import Config
from .models import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreCreatedResponse,
    ChoreDetail,
    ChoreReq,
    ChoreSummary,
    DonetickChore,
    ErrorResponse,
    FrequencyType,
    HistorySummary,
    Label,
    LabelSummary,
    MessageResponse,
    SubtaskSummary,
    TimerState,
    UserProfile,
    UserSummary,
)
from .transforms import (
    build_frequency_metadata,
    build_notification_metadata,
    build_subtasks,
    detail_of,
    ensure_assignee_consistency,
    history_status_label,
    iso,
    labels_by_name,
    parse_due_date,
    resolve_timezone,
    summary_of,
    user_summary_of,
)

log = logging.getLogger(__name__)

#: Frequent answers to "which strategies/types are valid", used in docstrings
#: so the generated tool schemas are self-describing.
STRATEGIES = (
    "no_assignee, random, least_assigned, least_completed, keep_last_assigned, "
    "random_except_last_assigned, round_robin"
)
FREQUENCIES = (
    "once, daily, weekly, monthly, yearly, interval, days_of_the_week, "
    "day_of_the_month, adaptive, trigger, no_repeat"
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Build a single authenticated client for the server's lifetime."""
    config = Config.from_env()
    logging.getLogger(__name__).setLevel(config.log_level)
    client = DonetickClient(config)
    try:
        # Authenticate eagerly so a bad password fails at startup rather than
        # on the first tool call.
        await client.ensure_auth()
        yield {"client": client, "config": config}
    finally:
        await client.aclose()


mcp = FastMCP(
    "Donetick",
    instructions=(
        "Manage household chores in Donetick. Resolve user IDs with the "
        "donetick://users resource or list_users before assigning anyone. "
        "Prefer delete_chore only when the user asks to remove a chore "
        "permanently; archive_chore is reversible and usually what is meant."
    ),
    lifespan=lifespan,
)


def _client(ctx: Context) -> DonetickClient:
    """Retrieve the shared client from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


def _config(ctx: Context) -> Config:
    return ctx.request_context.lifespan_context["config"]


def _tz(ctx: Context):
    return resolve_timezone(_config(ctx).timezone)


def _due_date(ctx: Context, value: str | None) -> datetime | None:
    """Parse a user-supplied due date in the server's configured timezone."""
    if value is None:
        return None
    try:
        return parse_due_date(value, _tz(ctx))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _unwrap_error(exc: Exception, message: str) -> ErrorResponse:
    log.warning("%s: %s", message, exc)
    return ErrorResponse(error=f"{message}: {exc}")


def _json_resource(payload: Any) -> str:
    """Serialise a resource payload.

    MCP resources are text, so structured payloads are JSON-encoded.  A
    discriminated envelope keeps the error case parseable alongside successes.
    """
    return json.dumps(payload, default=str)


async def _resolve_user_ids(
    ctx: Context, ids: list[int] | None, usernames: list[str] | None
) -> list[int]:
    """Combine explicit user IDs with usernames resolved to IDs.

    Raises:
        ValueError: If a username is not a member of the circle.
    """
    resolved = list(ids or [])
    if not usernames:
        return resolved
    members = {m.username.lower(): m.user_id for m in await _client(ctx).get_circle_members()}
    for name in usernames:
        key = name.strip().lower()
        if key not in members:
            valid = ", ".join(sorted(members)) or "(none)"
            msg = f"Unknown user {name!r}. Circle members: {valid}"
            raise ValueError(msg)
        resolved.append(members[key])
    return list(dict.fromkeys(resolved))


async def _chore_or_error(ctx: Context, chore_id: int) -> DonetickChore:
    """Fetch a chore, raising a clear error when it is missing.

    Raises:
        DonetickNotFoundError: If the chore does not exist.
    """
    chore = await _client(ctx).get_chore(chore_id)
    if chore is None:
        msg = f"Chore {chore_id} not found"
        raise DonetickNotFoundError(msg)
    return chore


# ---------------------------------------------------------------------------
# Chores
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_chores(
    ctx: Context,
    include_archived: bool = False,
) -> list[ChoreSummary] | ErrorResponse:
    """List chores in the circle.

    Args:
        include_archived: Include archived chores in the results.
    """
    client = _client(ctx)
    try:
        chores = (
            await client.list_archived_chores()
            if include_archived
            else await client.list_chores()
        )
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to list chores")
    return [summary_of(chore) for chore in chores]


@mcp.tool()
async def search_chores(
    ctx: Context,
    query: str,
    include_archived: bool = False,
) -> list[ChoreSummary] | ErrorResponse:
    """Search chores by name, description or label (case-insensitive).

    Args:
        query: Text to look for.
        include_archived: Also search archived chores.
    """
    client = _client(ctx)
    try:
        active = await client.list_chores()
        archived = await client.list_archived_chores() if include_archived else []
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to search chores")

    needle = query.strip().lower()
    if not needle:
        return [summary_of(chore) for chore in active]
    matches = [
        chore
        for chore in (*active, *archived)
        if needle in chore.name.lower()
        or (chore.description and needle in chore.description.lower())
        or any(needle in label.name.lower() for label in chore.labels_v2 or [])
    ]
    return [summary_of(chore) for chore in matches]


@mcp.tool()
async def get_chore(ctx: Context, chore_id: int) -> ChoreDetail | ErrorResponse:
    """Get the full details of a single chore.

    Args:
        chore_id: The chore's ID.
    """
    try:
        return detail_of(await _chore_or_error(ctx, chore_id))
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to get chore {chore_id}")


@mcp.tool()
async def create_chore(
    ctx: Context,
    name: str,
    description: str | None = None,
    due_date: str | None = None,
    frequency_type: str = "once",
    frequency: int = 1,
    assigned_to: int | None = None,
    assigned_username: str | None = None,
    assignees: list[int] | None = None,
    assignee_usernames: list[str] | None = None,
    assign_strategy: str = "no_assignee",
    days_of_week: list[str] | None = None,
    months: list[str] | None = None,
    time_of_day: str | None = None,
    is_rolling: bool = False,
    priority: int = 0,
    points: int | None = None,
    completion_window: int | None = None,
    labels: list[str] | None = None,
    subtasks: list[str] | None = None,
    notification: bool = False,
    remind_minutes_before: int | None = None,
    remind_at_due_time: bool = False,
    require_approval: bool = False,
) -> ChoreCreatedResponse | ErrorResponse:
    """Create a chore.

    Args:
        name: Chore name.
        description: Optional longer description.
        due_date: Due date. Accepts a date (``2026-12-25``), a time of day
            (``19:00``), or a full ISO 8601 timestamp. Naive values are read in
            the server's configured timezone.
        frequency_type: One of ``once, daily, weekly, monthly, yearly,
            interval, days_of_the_week, day_of_the_month, adaptive, trigger,
            no_repeat``.
        frequency: Interval, meaning depends on ``frequency_type``. For
            ``day_of_the_month`` it is the day of the month (1-31). Minimum 1;
            0 is rejected because it silently breaks rescheduling.
        assigned_to: User ID currently responsible for the chore.
        assigned_username: Username currently responsible, instead of an ID.
        assignees: User IDs eligible to be assigned.
        assignee_usernames: Usernames eligible to be assigned.
        assign_strategy: One of ``no_assignee, random, least_assigned,
            least_completed, keep_last_assigned, random_except_last_assigned,
            round_robin``.
        days_of_week: Required for ``days_of_the_week``. Names like ``mon`` or
            ``monday`` are accepted.
        months: Required for ``day_of_the_month``. Names like ``jan`` or
            ``january`` are accepted.
        time_of_day: Local time of day (``HH:MM``) for scheduled recurrences.
        is_rolling: Roll the due date forward from completion instead of
            from the current due date. Requires ``due_date``.
        priority: Priority level, 0 (none) through 4 (highest).
        points: Point value awarded on completion.
        completion_window: Seconds after the due date during which completion
            counts as on time.
        labels: Existing label names to attach. Create new ones with
            ``create_label`` first.
        subtasks: Checklist item names.
        notification: Enable reminders for this chore.
        remind_minutes_before: Minutes before the due date to remind. Up to 5
            reminders in total.
        remind_at_due_time: Also remind when the chore becomes due.
        require_approval: Require approval before the completion counts.
    """
    client = _client(ctx)
    try:
        assignee_ids = await _resolve_user_ids(
            ctx, assignees, assignee_usernames
        )
        if assigned_username and assigned_to is None:
            resolved = await _resolve_user_ids(ctx, None, [assigned_username])
            assigned_to = resolved[0]

        freq_type = FrequencyType(frequency_type)
        metadata = build_frequency_metadata(
            freq_type,
            days=days_of_week,
            months=months,
            time_of_day=time_of_day,
            timezone=_config(ctx).timezone,
        )
        notifications = build_notification_metadata(
            remind_minutes_before=remind_minutes_before,
            remind_at_due_time=remind_at_due_time,
        )

        label_entries: list[Label] | None = None
        if labels:
            catalog = await client.get_label_catalog()
            label_entries = labels_by_name(None, add=labels, catalog=catalog)

        req = ChoreReq(
            name=name,
            description=description or "",
            next_due_date=_due_date(ctx, due_date),
            frequency_type=freq_type,
            frequency=frequency,
            frequency_metadata=metadata,
            assignees=[ChoreAssignees(user_id=uid) for uid in assignee_ids],
            assigned_to=assigned_to,
            assign_strategy=AssignmentStrategy(assign_strategy),
            is_rolling=is_rolling,
            priority=priority,
            points=points,
            completion_window=completion_window,
            labels_v2=label_entries,
            sub_tasks=build_subtasks(subtasks) if subtasks else None,
            # Requesting reminders implies turning notifications on, otherwise
            # Donetick stores the metadata but never sends anything.
            notification=notification or notifications is not None,
            notification_metadata=notifications,
            require_approval=require_approval,
        )
        ensure_assignee_consistency(req)
        chore_id = await client.create_chore(req)
    except (DonetickError, ValueError) as exc:
        return _unwrap_error(exc, f"Failed to create chore {name!r}")

    return ChoreCreatedResponse(
        id=chore_id, name=name, message=f"Chore {name!r} created"
    )


@mcp.tool()
async def update_chore(
    ctx: Context,
    chore_id: int,
    name: str | None = None,
    description: str | None = None,
    due_date: str | None = None,
    frequency_type: str | None = None,
    frequency: int | None = None,
    days_of_week: list[str] | None = None,
    months: list[str] | None = None,
    time_of_day: str | None = None,
    assigned_to: int | None = None,
    assigned_username: str | None = None,
    assignees: list[int] | None = None,
    assignee_usernames: list[str] | None = None,
    assign_strategy: str | None = None,
    is_rolling: bool | None = None,
    priority: int | None = None,
    points: int | None = None,
    completion_window: int | None = None,
    is_active: bool | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    set_labels: list[str] | None = None,
    add_subtasks: list[str] | None = None,
    remove_subtasks: list[str] | None = None,
    notification: bool | None = None,
    remind_minutes_before: int | None = None,
    remind_at_due_time: bool | None = None,
    require_approval: bool | None = None,
) -> MessageResponse | ErrorResponse:
    """Update a chore. Only the fields you pass are changed.

    Donetick's update endpoint replaces the whole record, so unspecified fields
    are read from the current chore first and written back unchanged.

    Args:
        chore_id: The chore's ID.
        name: New name.
        description: New description.
        due_date: New due date. Accepts a date, a time of day, or an ISO 8601
            timestamp, interpreted in the configured timezone.
        frequency_type: New recurrence type.
        frequency: New interval. For ``day_of_the_month`` this is the day of
            the month.
        days_of_week: Days to repeat on, for ``days_of_the_week``.
        months: Months to repeat in, for ``day_of_the_month``.
        time_of_day: Local time of day (``HH:MM``) for scheduled recurrences.
        assigned_to: New responsible user ID.
        assigned_username: New responsible username, instead of an ID.
        assignees: New list of eligible assignee IDs.
        assignee_usernames: New list of eligible assignee usernames.
        assign_strategy: New assignment strategy.
        is_rolling: Whether the due date rolls from completion.
        priority: New priority, 0 through 4.
        points: New point value.
        completion_window: New on-time window in seconds.
        is_active: Set False to deactivate without archiving.
        add_labels: Label names to add.
        remove_labels: Label names to remove.
        set_labels: Replace all labels with these names.
        add_subtasks: Checklist item names to append.
        remove_subtasks: Exact names of checklist items to delete.
        notification: Enable or disable reminders.
        remind_minutes_before: Minutes before the due date to remind.
        remind_at_due_time: Also remind when the chore becomes due.
        require_approval: Require approval before completion counts.
    """
    client = _client(ctx)
    try:
        chore = await _chore_or_error(ctx, chore_id)
        # The caller's due date is threaded into the payload instead of being
        # assigned onto it afterwards: a rolling chore whose stored date went
        # missing fails `ChoreReq` validation while the payload is still being
        # built, which would leave that chore uneditable -- and impossible to
        # repair.  `_due_date` returns None when no date was supplied, which
        # keeps the stored one.
        req = await client.chore_to_req(chore, next_due_date=_due_date(ctx, due_date))

        if name is not None:
            req.name = name
        if description is not None:
            req.description = description
        if frequency_type is not None:
            req.frequency_type = FrequencyType(frequency_type)
        if frequency is not None:
            req.frequency = frequency
        if days_of_week is not None or months is not None or time_of_day is not None:
            req.frequency_metadata = build_frequency_metadata(
                req.frequency_type,
                days=days_of_week,
                months=months,
                time_of_day=time_of_day,
                timezone=_config(ctx).timezone,
            )
        if assigned_to is not None:
            req.assigned_to = assigned_to
        if assigned_username:
            resolved = await _resolve_user_ids(ctx, None, [assigned_username])
            req.assigned_to = resolved[0]
        if assignees is not None or assignee_usernames is not None:
            ids = await _resolve_user_ids(ctx, assignees, assignee_usernames)
            req.assignees = [ChoreAssignees(user_id=uid) for uid in ids]
        if assign_strategy is not None:
            req.assign_strategy = AssignmentStrategy(assign_strategy)
        if is_rolling is not None:
            req.is_rolling = is_rolling
        if priority is not None:
            req.priority = priority
        if points is not None:
            req.points = points
        if completion_window is not None:
            req.completion_window = completion_window
        if is_active is not None:
            req.is_active = is_active
        if add_labels or remove_labels or set_labels is not None:
            catalog = await client.get_label_catalog()
            req.labels_v2 = labels_by_name(
                chore,
                add=add_labels,
                remove=remove_labels,
                set_names=set_labels,
                catalog=catalog,
            )
        if add_subtasks or remove_subtasks:
            existing = list(req.sub_tasks or [])
            if remove_subtasks:
                removed = set(remove_subtasks)
                existing = [task for task in existing if task.name not in removed]
            if add_subtasks:
                start = len(existing)
                existing += build_subtasks(add_subtasks)
                for offset, task in enumerate(existing[start:], start=start):
                    task.order_id = offset
            req.sub_tasks = existing
        if notification is not None:
            req.notification = notification
        if remind_minutes_before is not None or remind_at_due_time is not None:
            metadata = build_notification_metadata(
                remind_minutes_before=remind_minutes_before,
                remind_at_due_time=remind_at_due_time,
            )
            req.notification_metadata = metadata
            if metadata is not None:
                req.notification = True
        if require_approval is not None:
            req.require_approval = require_approval

        ensure_assignee_consistency(req)
        warnings = await client.update_chore(req)
    except (DonetickError, ValueError) as exc:
        return _unwrap_error(exc, f"Failed to update chore {chore_id}")

    message = f"Chore {chore_id} updated"
    if warnings:
        message = f"{message} (warnings: {'; '.join(warnings)})"
    return MessageResponse(message=message)


@mcp.tool()
async def complete_chore(
    ctx: Context,
    chore_id: int,
    note: str | None = None,
) -> ChoreDetail | ErrorResponse:
    """Mark a chore as done. Recurring chores reschedule themselves.

    Args:
        chore_id: The chore's ID.
        note: Optional note recorded with the completion.
    """
    client = _client(ctx)
    try:
        updated = await client.complete_chore(chore_id, note=note)
        return detail_of(updated)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to complete chore {chore_id}")


@mcp.tool()
async def skip_chore(
    ctx: Context,
    chore_id: int,
    note: str | None = None,
) -> MessageResponse | ErrorResponse:
    """Skip the current occurrence of a chore without marking it done.

    Args:
        chore_id: The chore's ID.
        note: Optional reason for the skip.
    """
    try:
        await _client(ctx).skip_chore(chore_id, note=note)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to skip chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} skipped")


@mcp.tool()
async def approve_chore(ctx: Context, chore_id: int) -> MessageResponse | ErrorResponse:
    """Approve a chore completion that was awaiting approval.

    Args:
        chore_id: The chore's ID.
    """
    try:
        await _client(ctx).approve_chore(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to approve chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} approved")


@mcp.tool()
async def reject_chore(
    ctx: Context,
    chore_id: int,
    reason: str | None = None,
) -> MessageResponse | ErrorResponse:
    """Reject a chore completion, sending it back to the assignee.

    Args:
        chore_id: The chore's ID.
        reason: Optional explanation shown to the assignee.
    """
    try:
        await _client(ctx).reject_chore(chore_id, reason=reason)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to reject chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} rejected")


@mcp.tool()
async def update_due_date(
    ctx: Context, chore_id: int, due_date: str
) -> MessageResponse | ErrorResponse:
    """Move a chore's due date without rewriting the rest of the record.

    Args:
        chore_id: The chore's ID.
        due_date: The new due date, in the configured timezone if naive.
    """
    try:
        parsed = _due_date(ctx, due_date)
        if parsed is None:
            msg = "due_date must not be empty"
            raise ValueError(msg)
        await _client(ctx).update_due_date(chore_id, parsed)
    except (DonetickError, ValueError) as exc:
        return _unwrap_error(exc, f"Failed to set due date for chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} due date set to {due_date}")


# ---------------------------------------------------------------------------
# Archival and deletion
# ---------------------------------------------------------------------------


@mcp.tool()
async def archive_chore(ctx: Context, chore_id: int) -> MessageResponse | ErrorResponse:
    """Archive a chore. Reversible, and it disappears from the default list.

    Args:
        chore_id: The chore's ID.
    """
    try:
        await _client(ctx).archive_chore(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to archive chore {chore_id}")
    return MessageResponse(
        message=f"Chore {chore_id} archived (recoverable with unarchive_chore)"
    )


@mcp.tool()
async def unarchive_chore(ctx: Context, chore_id: int) -> MessageResponse | ErrorResponse:
    """Restore an archived chore.

    Args:
        chore_id: The chore's ID.
    """
    try:
        await _client(ctx).unarchive_chore(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to unarchive chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} restored")


@mcp.tool()
async def list_archived_chores(ctx: Context) -> list[ChoreSummary] | ErrorResponse:
    """List archived chores."""
    try:
        chores = await _client(ctx).list_archived_chores()
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to list archived chores")
    return [summary_of(chore) for chore in chores]


@mcp.tool()
async def delete_chore(
    ctx: Context,
    chore_id: int,
    confirm: bool = False,
) -> MessageResponse | ErrorResponse:
    """Permanently delete a chore and its history. This cannot be undone.

    Args:
        chore_id: The chore's ID.
        confirm: Must be True to proceed. Guards against accidental deletion;
            use ``archive_chore`` if the chore may be needed again.
    """
    if not confirm:
        return ErrorResponse(
            error=(
                f"Refusing to permanently delete chore {chore_id} without "
                "confirm=True. Use archive_chore if it may be needed again."
            )
        )
    try:
        await _client(ctx).delete_chore(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to delete chore {chore_id}")
    return MessageResponse(message=f"Chore {chore_id} permanently deleted")


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_chore_timer(ctx: Context, chore_id: int) -> TimerState | ErrorResponse:
    """Start timing work on a chore.

    Fails if the timer is already running.

    Args:
        chore_id: The chore's ID.
    """
    try:
        return await _client(ctx).start_timer(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to start timer for chore {chore_id}")


@mcp.tool()
async def pause_chore_timer(ctx: Context, chore_id: int) -> TimerState | ErrorResponse:
    """Pause the timer on a chore.

    Args:
        chore_id: The chore's ID.
    """
    try:
        return await _client(ctx).pause_timer(chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to pause timer for chore {chore_id}")


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_subtasks(
    ctx: Context, chore_id: int
) -> list[SubtaskSummary] | ErrorResponse:
    """List a chore's checklist items.

    Args:
        chore_id: The chore's ID.
    """
    try:
        chore = await _chore_or_error(ctx, chore_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to get chore {chore_id}")
    return [
        SubtaskSummary(
            id=task.id,
            name=task.name,
            order=task.order_id,
            completed=task.completed_at is not None,
            completed_at=iso(task.completed_at),
        )
        for task in chore.sub_tasks or []
    ]


@mcp.tool()
async def create_subtask(
    ctx: Context,
    chore_id: int,
    name: str,
) -> MessageResponse | ErrorResponse:
    """Add a checklist item to a chore.

    Args:
        chore_id: The chore's ID.
        name: The checklist item's name.
    """
    client = _client(ctx)
    try:
        chore = await _chore_or_error(ctx, chore_id)
        await client.replace_subtasks(
            chore,
            [*(chore.sub_tasks or []), *build_subtasks([name])],
        )
    except (DonetickError, ValueError) as exc:
        return _unwrap_error(exc, f"Failed to add subtask to chore {chore_id}")
    return MessageResponse(message=f"Subtask {name!r} added to chore {chore_id}")


@mcp.tool()
async def delete_subtask(
    ctx: Context,
    chore_id: int,
    name: str | None = None,
    subtask_id: int | None = None,
) -> MessageResponse | ErrorResponse:
    """Delete a checklist item, identified by name or by ID.

    Args:
        chore_id: The chore's ID.
        name: Name of the item to delete.
        subtask_id: ID of the item to delete, used when the name is ambiguous.
    """
    client = _client(ctx)
    try:
        chore = await _chore_or_error(ctx, chore_id)
        tasks = list(chore.sub_tasks or [])
        if subtask_id is not None:
            tasks = [task for task in tasks if task.id != subtask_id]
        elif name is not None:
            tasks = [task for task in tasks if task.name != name]
        else:
            return ErrorResponse(error="Provide either name or subtask_id")
        if len(tasks) == len(chore.sub_tasks or []):
            wanted = subtask_id if subtask_id is not None else name
            return ErrorResponse(error=f"No subtask matching {wanted!r} on chore {chore_id}")
        await client.replace_subtasks(chore, tasks)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to delete subtask on chore {chore_id}")
    return MessageResponse(message=f"Subtask deleted from chore {chore_id}")


@mcp.tool()
async def update_subtask_completion(
    ctx: Context,
    chore_id: int,
    subtask_id: int,
    completed: bool = True,
) -> MessageResponse | ErrorResponse:
    """Mark a checklist item complete, or clear it again.

    Args:
        chore_id: The chore's ID.
        subtask_id: The checklist item's ID.
        completed: True to mark complete, False to clear.
    """
    try:
        await _client(ctx).set_subtask_completion(
            chore_id, subtask_id, datetime.now().astimezone().isoformat() if completed else None
        )
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to update subtask {subtask_id}")
    state = "completed" if completed else "reopened"
    return MessageResponse(message=f"Subtask {subtask_id} {state}")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_labels(ctx: Context) -> list[LabelSummary] | ErrorResponse:
    """List the circle's labels."""
    try:
        labels = await _client(ctx).list_labels()
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to list labels")
    return [LabelSummary(id=label.id, name=label.name, color=label.color or "") for label in labels]


@mcp.tool()
async def create_label(
    ctx: Context, name: str, color: str = "#CCCCCC"
) -> LabelSummary | ErrorResponse:
    """Create a label.

    Args:
        name: Label name, unique within the circle.
        color: Hex colour including the leading ``#``.
    """
    try:
        label = await _client(ctx).create_label(name, color)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to create label {name!r}")
    return LabelSummary(id=label.id, name=label.name, color=label.color or "")


@mcp.tool()
async def update_label(
    ctx: Context, label_id: int, name: str, color: str
) -> LabelSummary | ErrorResponse:
    """Rename or recolour a label.

    Args:
        label_id: The label's ID.
        name: New name.
        color: New hex colour including the leading ``#``.
    """
    try:
        label = await _client(ctx).update_label(label_id, name, color)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to update label {label_id}")
    return LabelSummary(id=label.id, name=label.name, color=label.color or "")


@mcp.tool()
async def delete_label(ctx: Context, label_id: int) -> MessageResponse | ErrorResponse:
    """Delete a label, detaching it from every chore that used it.

    Args:
        label_id: The label's ID.
    """
    try:
        await _client(ctx).delete_label(label_id)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to delete label {label_id}")
    return MessageResponse(message=f"Label {label_id} deleted")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_chore_history(
    ctx: Context, chore_id: int, duration_days: int = 30
) -> list[HistorySummary] | ErrorResponse:
    """Get a single chore's completion history.

    Args:
        chore_id: The chore's ID.
        duration_days: How far back to look.
    """
    try:
        entries = await _client(ctx).get_chore_history(chore_id, duration_days)
    except DonetickError as exc:
        return _unwrap_error(exc, f"Failed to get history for chore {chore_id}")
    return [
        HistorySummary(
            id=entry.id,
            chore_id=entry.chore_id or chore_id,
            performed_at=iso(entry.performed_at),
            completed_by=entry.completed_by,
            notes=entry.notes,
            due_date=iso(entry.due_date),
            status=entry.status,
            status_label=history_status_label(entry.status),
        )
        for entry in entries
    ]


@mcp.tool()
async def get_history(
    ctx: Context,
    duration_days: int = 7,
    include_circle: bool = False,
) -> list[HistorySummary] | ErrorResponse:
    """Get recent completion history across all chores.

    Args:
        duration_days: How far back to look.
        include_circle: Include other members' activity, not just your own.
    """
    try:
        entries = await _client(ctx).get_history(duration_days, include_circle)
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to get history")
    return [
        HistorySummary(
            id=entry.id,
            chore_id=entry.chore_id,
            performed_at=iso(entry.performed_at),
            completed_by=entry.completed_by,
            notes=entry.notes,
            due_date=iso(entry.due_date),
            status=entry.status,
            status_label=history_status_label(entry.status),
        )
        for entry in entries
    ]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_profile(ctx: Context) -> UserProfile | ErrorResponse:
    """Get the authenticated user's profile."""
    try:
        return await _client(ctx).get_profile()
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to get profile")


@mcp.tool()
async def list_users(ctx: Context) -> list[UserSummary] | ErrorResponse:
    """List the circle's members with their roles and points."""
    try:
        members = await _client(ctx).get_circle_members()
    except DonetickError as exc:
        return _unwrap_error(exc, "Failed to list users")
    return [
        UserSummary(
            id=member.user_id,
            display_name=member.display_name,
            username=member.username,
            email="",
            role=member.role,
            points=member.points,
        )
        for member in members
    ]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("donetick://users")
async def users_resource(ctx: Context) -> str:
    """Circle members. Use this to resolve user IDs before assigning a chore."""
    try:
        members = await _client(ctx).get_circle_members()
    except DonetickError as exc:
        return _json_resource({"error": str(exc)})
    return _json_resource([user_summary_of(member) for member in members])


@mcp.resource("donetick://labels")
async def labels_resource(ctx: Context) -> str:
    """Circle labels available for chore assignment."""
    try:
        labels = await _client(ctx).list_labels()
    except DonetickError as exc:
        return _json_resource({"error": str(exc)})
    return _json_resource(
        [
            {"id": label.id, "name": label.name, "color": label.color}
            for label in labels
        ]
    )


@mcp.resource("donetick://chore/{chore_id}")
async def chore_resource(ctx: Context, chore_id: int) -> str:
    """Full detail of one chore, for attaching context without a tool call."""
    try:
        return _json_resource(detail_of(await _chore_or_error(ctx, chore_id)).model_dump())
    except DonetickError as exc:
        return _json_resource({"error": f"Chore {chore_id} not found: {exc}"})


@mcp.resource("donetick://profile")
async def profile_resource(ctx: Context) -> str:
    """The authenticated user's profile."""
    try:
        return _json_resource((await _client(ctx).get_profile()).model_dump())
    except DonetickError as exc:
        return _json_resource({"error": str(exc)})


@mcp.resource("donetick://chores")
async def chores_resource(ctx: Context) -> str:
    """Every active chore, as a summary list."""
    try:
        chores = await _client(ctx).list_chores()
    except DonetickError as exc:
        return _json_resource({"error": str(exc)})
    return _json_resource([summary_of(chore).model_dump() for chore in chores])


#: The MCP server object.  Import this to run or embed the server; every
#: public type is re-exported from :mod:`donetick_mcp` instead.
__all__ = ["mcp"]
