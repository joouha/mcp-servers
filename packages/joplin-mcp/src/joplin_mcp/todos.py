"""Pure helpers for Joplin's to-do state.

Joplin stores to-do metadata as raw epoch milliseconds (``todo_due`` and
``todo_completed``), where ``0`` means "no due date" or "still open". These
helpers parse, render, and filter on those values. Nothing here imports the
rest of the package or touches the network, so the module is testable on its
own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

#: Accepted values for the ``todo`` filter argument of the listing tools.
TODO_STATES = ("all", "open", "done")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_iso() -> str:
    """Current time in Joplin's own format: UTC, millisecond precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def now_ms() -> int:
    """Current time as epoch milliseconds (Joplin's raw to-do format)."""
    return int(datetime.now(UTC).timestamp() * 1000)


def coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int from Joplin metadata: tolerates ``''``, ``None``, junk."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def ms_to_date(ms: int) -> str:
    """Render epoch milliseconds as a UTC ``YYYY-MM-DD`` date."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def parse_due(due: str) -> int:
    """Parse ``YYYY-MM-DD`` or an ISO-8601 timestamp to epoch ms; ``''`` -> 0.

    Bare dates are interpreted as UTC midnight. Raises ``ValueError`` on junk.
    """
    due = due.strip()
    if not due:
        return 0
    if _DATE_RE.match(due):
        dt = datetime.strptime(due, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        try:
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = f"Invalid due date: '{due}'. Use YYYY-MM-DD or an ISO-8601 timestamp."
            raise ValueError(msg) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _field(note: dict[str, Any], key: str) -> Any:
    """Read a metadata field from a parsed item, tolerating stale caches."""
    if key in note:
        return note.get(key)
    return note.get("metadata", {}).get(key, 0)


def due_value(note: dict[str, Any]) -> int:
    """``todo_due`` in epoch ms; ``0`` means no due date."""
    return coerce_int(_field(note, "todo_due"))


def completed_value(note: dict[str, Any]) -> int:
    """``todo_completed`` in epoch ms; ``0`` means the to-do is still open."""
    return coerce_int(_field(note, "todo_completed"))


def todo_marker(note: dict[str, Any]) -> str:
    """Plain-ASCII to-do marker for list output.

    One of ``''`` (not a to-do), ``[done YYYY-MM-DD]``, or
    ``[todo] (due YYYY-MM-DD)`` with a trailing ``OVERDUE`` when the date has
    passed.
    """
    if not note.get("is_todo"):
        return ""
    completed = completed_value(note)
    if completed:
        return f"[done {ms_to_date(completed)}]"
    marker = "[todo]"
    due = due_value(note)
    if due:
        marker += f" (due {ms_to_date(due)})"
        if due < now_ms():
            marker += " OVERDUE"
    return marker


def filter_by_todo(
    notes: list[dict[str, Any]], todo: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Filter notes by to-do state. Returns ``(filtered, error)``.

    ``todo`` is ``None`` for no filtering, or one of :data:`TODO_STATES`:
    ``'all'`` (any to-do), ``'open'`` (uncompleted), ``'done'`` (completed).
    The error is ``None`` on success, otherwise a message for the caller.
    """
    if todo is None:
        return notes, None
    key = todo.strip().lower()
    if key not in TODO_STATES:
        return notes, "todo must be 'all', 'open', or 'done'."
    if key == "all":
        return [n for n in notes if n.get("is_todo")], None
    want_completed = key == "done"
    return (
        [
            n
            for n in notes
            if n.get("is_todo") and bool(completed_value(n)) == want_completed
        ],
        None,
    )
