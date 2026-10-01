"""Donetick MCP Server.

An MCP server for managing household chores via the Donetick API.

The implementation is split across focused modules:

* :mod:`donetick_mcp.config` - environment-driven configuration
* :mod:`donetick_mcp.models` - request/response schemas
* :mod:`donetick_mcp.transforms` - argument-to-payload conversion
* :mod:`donetick_mcp.client` - async, rate-limited, retrying HTTP client
* :mod:`donetick_mcp.server` - FastMCP tools and resources

Everything public is re-exported here for convenience.
"""

from __future__ import annotations

from .client import (
    DonetickAuthError,
    DonetickClient,
    DonetickError,
    DonetickNotFoundError,
    DonetickTransportError,
    TokenBucket,
)
from .config import Config
from .models import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreCreatedResponse,
    ChoreDetail,
    ChoreHistoryEntry,
    ChoreHistoryStatus,
    ChoreReq,
    ChoreStatus,
    ChoreSummary,
    CircleMember,
    DonetickChore,
    ErrorResponse,
    FrequencyMetadata,
    FrequencyType,
    HistorySummary,
    Label,
    LabelSummary,
    MessageResponse,
    NotificationMetadata,
    SubTask,
    SubtaskSummary,
    TimerState,
    UserProfile,
    UserSummary,
)
from .server import mcp
from .transforms import (
    build_frequency_metadata,
    build_notification_metadata,
    build_subtasks,
    chore_status_label,
    detail_of,
    ensure_assignee_consistency,
    history_status_label,
    iso,
    labels_by_name,
    labels_for_update,
    normalise_days,
    normalise_months,
    parse_due_date,
    resolve_timezone,
    summary_of,
    user_summary_of,
)

__all__ = [
    "AssignmentStrategy",
    "ChoreAssignees",
    "ChoreCreatedResponse",
    "ChoreDetail",
    "ChoreHistoryEntry",
    "ChoreHistoryStatus",
    "ChoreReq",
    "ChoreStatus",
    "ChoreSummary",
    "CircleMember",
    "Config",
    "DonetickAuthError",
    "DonetickChore",
    "DonetickClient",
    "DonetickError",
    "DonetickNotFoundError",
    "DonetickTransportError",
    "ErrorResponse",
    "FrequencyMetadata",
    "FrequencyType",
    "HistorySummary",
    "Label",
    "LabelSummary",
    "MessageResponse",
    "NotificationMetadata",
    "SubTask",
    "SubtaskSummary",
    "TimerState",
    "TokenBucket",
    "UserProfile",
    "UserSummary",
    "build_frequency_metadata",
    "build_notification_metadata",
    "build_subtasks",
    "chore_status_label",
    "detail_of",
    "ensure_assignee_consistency",
    "history_status_label",
    "iso",
    "labels_by_name",
    "labels_for_update",
    "main",
    "mcp",
    "normalise_days",
    "normalise_months",
    "parse_due_date",
    "resolve_timezone",
    "summary_of",
    "user_summary_of",
]


def main() -> None:
    """Run the Donetick MCP server over stdio."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()