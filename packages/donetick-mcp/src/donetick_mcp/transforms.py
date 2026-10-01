"""Helpers for translating between MCP tool arguments and Donetick payloads."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreDetail,
    ChoreHistoryStatus,
    ChoreReq,
    ChoreStatus,
    ChoreSummary,
    CircleMember,
    DonetickChore,
    FrequencyMetadata,
    FrequencyType,
    Label,
    NotificationMetadata,
    SubTask,
)

#: Accepted spellings for weekdays, mapped to Donetick's lowercase full names.
DAY_ALIASES: dict[str, str] = {
    "mon": "monday",
    "monday": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "wed": "wednesday",
    "weds": "wednesday",
    "wednesday": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fri": "friday",
    "friday": "friday",
    "sat": "saturday",
    "saturday": "saturday",
    "sun": "sunday",
    "sunday": "sunday",
}

MONTH_ALIASES: dict[str, str] = {
    "jan": "january",
    "january": "january",
    "feb": "february",
    "february": "february",
    "mar": "march",
    "march": "march",
    "apr": "april",
    "april": "april",
    "may": "may",
    "jun": "june",
    "june": "june",
    "jul": "july",
    "july": "july",
    "aug": "august",
    "august": "august",
    "sep": "september",
    "sept": "september",
    "september": "september",
    "oct": "october",
    "october": "october",
    "nov": "november",
    "november": "november",
    "dec": "december",
    "december": "december",
}


def resolve_timezone(name: str) -> ZoneInfo:
    """Return a :class:`ZoneInfo` for ``name``.

    Falls back to UTC with a warning when the name is unknown, so a
    misconfigured timezone degrades gracefully instead of failing the call.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        import logging

        logging.getLogger(__name__).warning(
            "Unknown timezone %r; falling back to UTC", name
        )
        return ZoneInfo("UTC")


def parse_due_date(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse a user-supplied due date into an aware datetime.

    Accepts ``YYYY-MM-DD``, ``HH:MM``, or any ISO 8601 datetime.  Naive values
    are interpreted in ``tz``.  Bare dates and bare times resolve to the next
    sensible occurrence relative to *now*.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    value = value.strip()
    if not value:
        return None

    now = datetime.now(tz)

    # Bare time of day, e.g. "19:00" -> today (or tomorrow) at that time.
    if _is_time_only(value):
        parsed_time = time.fromisoformat(value)
        candidate = datetime.combine(now.date(), parsed_time, tzinfo=tz)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # Bare date, e.g. "2025-11-10" -> local midnight.
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        pass
    else:
        return datetime.combine(parsed_date, time.min, tzinfo=tz)

    # Full ISO 8601, possibly with an explicit offset or trailing Z.
    normalised = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _is_time_only(value: str) -> bool:
    """Return True for ``HH:MM`` / ``HH:MM:SS`` style values."""
    parts = value.split(":")
    return len(parts) in (2, 3) and all(part.isdigit() for part in parts)


def iso(value: datetime | None) -> str | None:
    """Render a datetime as an ISO 8601 string, or None."""
    return value.isoformat() if value else None


def normalise_days(days: list[str]) -> list[str]:
    """Map user day names onto Donetick's lowercase full names.

    Raises:
        ValueError: If any day cannot be recognised.
    """
    normalised: list[str] = []
    unknown: list[str] = []
    for day in days:
        key = day.strip().lower()
        if key in DAY_ALIASES:
            normalised.append(DAY_ALIASES[key])
        else:
            unknown.append(day)
    if unknown:
        valid = "Mon/Monday, Tue/Tuesday, Wed/Wednesday, Thu/Thursday, Fri/Friday, Sat/Saturday, Sun/Sunday"
        msg = f"Invalid day name(s): {', '.join(unknown)}. Valid values: {valid}"
        raise ValueError(msg)
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(normalised))


def normalise_months(months: list[str]) -> list[str]:
    """Map user month names onto Donetick's lowercase full names.

    Raises:
        ValueError: If any month cannot be recognised.
    """
    normalised: list[str] = []
    unknown: list[str] = []
    for month in months:
        key = month.strip().lower()
        if key in MONTH_ALIASES:
            normalised.append(MONTH_ALIASES[key])
        else:
            unknown.append(month)
    if unknown:
        msg = f"Invalid month name(s): {', '.join(unknown)}"
        raise ValueError(msg)
    return list(dict.fromkeys(normalised))


def build_frequency_metadata(
    frequency_type: FrequencyType,
    *,
    days: list[str] | None = None,
    months: list[str] | None = None,
    time_of_day: str | None = None,
    timezone: str = "UTC",
    week_pattern: str | None = None,
) -> FrequencyMetadata | None:
    """Assemble ``frequencyMetadata`` from human-friendly arguments.

    Donetick's scheduler dereferences ``unit`` and ``timezone`` for several
    frequency types, so they are always populated.  ``days_of_the_week`` also
    requires the ``weekPattern``/``occurrences``/``weekNumbers`` trio.
    """
    if frequency_type not in (
        FrequencyType.DAYS_OF_THE_WEEK,
        FrequencyType.DAY_OF_THE_MONTH,
        FrequencyType.INTERVAL,
        FrequencyType.WEEKLY,
    ) and not (days or months or time_of_day):
        return None

    if frequency_type == FrequencyType.DAYS_OF_THE_WEEK and not days:
        msg = "days_of_week is required when frequency_type is 'days_of_the_week'"
        raise ValueError(msg)
    if frequency_type == FrequencyType.DAY_OF_THE_MONTH and not months:
        msg = "months is required when frequency_type is 'day_of_the_month'"
        raise ValueError(msg)

    metadata = FrequencyMetadata(
        timezone=timezone,
        unit="days",
        week_pattern=week_pattern or "every_week",
        occurrences=[],
        week_numbers=[],
    )
    if days:
        metadata.days = normalise_days(days)
    if months:
        metadata.months = normalise_months(months)
    if time_of_day:
        parsed = time.fromisoformat(time_of_day)
        tzinfo = resolve_timezone(timezone)
        now = datetime.now(tzinfo)
        metadata.time = datetime.combine(now.date(), parsed, tzinfo=tzinfo)
    return metadata


def build_notification_metadata(
    *,
    remind_minutes_before: int | None = None,
    remind_at_due_time: bool = False,
    nagging: bool = False,
) -> NotificationMetadata | None:
    """Assemble reminder settings, returning None when nothing is requested.

    Donetick caps a chore at five reminder templates, so callers should not
    generate more than that.

    Raises:
        ValueError: If more than five reminders are requested.
    """
    templates: list[dict[str, object]] = []
    if remind_minutes_before:
        templates.append({"value": int(remind_minutes_before), "unit": "m"})
    if remind_at_due_time:
        templates.append({"value": 0, "unit": "m"})
    if len(templates) > 5:
        msg = "a chore supports at most 5 reminders"
        raise ValueError(msg)
    if not templates and not nagging:
        return None
    return NotificationMetadata(
        nagging=nagging,
        predue=bool(templates),
        templates=templates or None,
    )


def build_subtasks(names: list[str]) -> list[SubTask]:
    """Build unsubmitted checklist items from plain names."""
    return [
        SubTask(order_id=index, name=name, completed_at=None, completed_by=0)
        for index, name in enumerate(names)
    ]


def summary_of(chore: DonetickChore) -> ChoreSummary:
    """Compact view of a chore for list/search results."""
    due = chore.next_due_date
    overdue = bool(
        due
        and due < datetime.now(UTC)
        and chore.is_active
        and chore.frequency_type
        not in (FrequencyType.ONCE, FrequencyType.NO_REPEAT, FrequencyType.TRIGGER)
    )
    return ChoreSummary(
        id=chore.id,
        name=chore.name,
        due_date=iso(due),
        active=chore.is_active,
        assigned_to=chore.assigned_to,
        assignees=[a.user_id for a in chore.assignees],
        frequency_type=chore.frequency_type.value,
        priority=chore.priority,
        points=chore.points,
        overdue=overdue,
    )


def detail_of(chore: DonetickChore) -> ChoreDetail:
    """Full view of a single chore."""
    return ChoreDetail(
        id=chore.id,
        name=chore.name,
        description=chore.description,
        due_date=iso(chore.next_due_date),
        active=chore.is_active,
        assigned_to=chore.assigned_to,
        assignees=[a.user_id for a in chore.assignees],
        assign_strategy=chore.assign_strategy.value,
        frequency=chore.frequency,
        frequency_type=chore.frequency_type.value,
        frequency_metadata=(
            chore.frequency_metadata.model_dump(mode="json", by_alias=True)
            if chore.frequency_metadata
            else None
        ),
        is_rolling=chore.is_rolling,
        priority=chore.priority,
        points=chore.points,
        completion_window=chore.completion_window,
        require_approval=chore.require_approval,
        is_private=chore.is_private,
        status=int(chore.status),
        status_label=chore_status_label(chore.status),
        labels=[label.name for label in chore.labels_v2] if chore.labels_v2 else [],
        sub_tasks=[
            {
                "id": task.id,
                "name": task.name,
                "completed": task.completed_at is not None,
                "completed_at": iso(task.completed_at),
            }
            for task in chore.sub_tasks or []
        ],
        created_at=iso(chore.created_at),
        updated_at=iso(chore.updated_at),
    )


def chore_status_label(status: int | ChoreStatus) -> str:
    """A readable name for a chore's lifecycle status.

    Unrecognised values pass through as-is rather than raising, so a Donetick
    release that adds a status cannot break reads.
    """
    try:
        return ChoreStatus(int(status)).name.lower()
    except ValueError:
        return f"unknown_{status}"


def history_status_label(status: int | None) -> str:
    """A readable name for a completion history outcome."""
    if status is None:
        return ""
    try:
        return ChoreHistoryStatus(int(status)).label
    except ValueError:
        return f"unknown_{status}"


def user_summary_of(member: CircleMember) -> dict[str, object]:
    """Flatten a circle member for the users resource."""
    return {
        "id": member.user_id,
        "display_name": member.display_name,
        "username": member.username,
        "role": member.role,
        "points": member.points,
    }


def labels_for_update(chore: DonetickChore) -> list[Label] | None:
    """Return labels in the shape ``labelsV2`` expects.

    Donetick validates each entry with ``binding:"required,gt=0"`` on the
    ``id`` field, so labels lacking one are dropped rather than sent as 0.
    """
    if not chore.labels_v2:
        return None
    return [
        Label(id=label.id, label_id=label.id, name=label.name, color=label.color)
        for label in chore.labels_v2
        if label.id > 0
    ]


def ensure_assignee_consistency(req: ChoreReq) -> None:
    """Keep ``assignedTo`` and ``assignees`` consistent before sending.

    Donetick rejects a payload whose ``assignedTo`` is absent from
    ``assignees`` with a 400, so add it.  Conversely, a strategy that implies
    rotation cannot be paired with an empty assignee list, so fall back to
    ``no_assignee`` in that case.
    """
    if req.assigned_to is not None:
        ids = [assignee.user_id for assignee in req.assignees]
        if req.assigned_to not in ids:
            req.assignees = [
                *req.assignees,
                ChoreAssignees(user_id=req.assigned_to),
            ]
    if req.assigned_to is None and not req.assignees:
        if req.assign_strategy != AssignmentStrategy.NO_ASSIGNEE:
            req.assign_strategy = AssignmentStrategy.NO_ASSIGNEE


def labels_by_name(
    existing: DonetickChore | None,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    set_names: list[str] | None = None,
    catalog: dict[str, int],
) -> list[Label] | None:
    """Resolve add/remove/set label operations into ``labelsV2`` entries.

    ``catalog`` maps lowercased label names to their IDs.

    Raises:
        ValueError: If a referenced label does not exist.
    """
    if add is None and remove is None and set_names is None:
        return None

    current: dict[str, int] = {}
    if existing is not None and existing.labels_v2:
        current = {label.name.lower(): label.id for label in existing.labels_v2 if label.id > 0}

    # `set_names` means "these labels and nothing else", so the current
    # selection is discarded entirely rather than intersected.
    if set_names is not None:
        current = {}
        add = [*(add or []), *set_names]

    for name in remove or []:
        current.pop(name.strip().lower(), None)

    for name in add or []:
        key = name.strip().lower()
        if key not in catalog:
            msg = f"Unknown label {name!r}. Create it with create_label first."
            raise ValueError(msg)
        current[key] = catalog[key]

    ordered = sorted(current.items(), key=lambda item: catalog.get(item[1], item[1]))
    return [
        Label(id=label_id, label_id=label_id, name=display_name)
        for display_name, label_id in ordered
    ]