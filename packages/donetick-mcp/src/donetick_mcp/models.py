"""Pydantic models describing the Donetick REST API.

The API speaks ``camelCase``.  Every model uses :func:`_to_camel` as its alias
generator so payloads round-trip correctly in both directions.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrequencyType(str, Enum):
    """Recurrence types accepted by Donetick's ``frequencyType`` field."""

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
    """Rotation strategies for recurring chores."""

    NO_ASSIGNEE = "no_assignee"
    RANDOM = "random"
    LEAST_ASSIGNED = "least_assigned"
    LEAST_COMPLETED = "least_completed"
    KEEP_LAST_ASSIGNED = "keep_last_assigned"
    RANDOM_EXCEPT_LAST_ASSIGNED = "random_except_last_assigned"
    ROUND_ROBIN = "round_robin"


class ChoreStatus(int, Enum):
    """Lifecycle state of a chore (``internal/chore/model/model.go``)."""

    NO_STATUS = 0
    IN_PROGRESS = 1
    PAUSED = 2
    PENDING_APPROVAL = 3


class ChoreHistoryStatus(int, Enum):
    """Outcome recorded against a completion (``ChoreHistoryStatus``)."""

    STARTED = 0
    COMPLETED = 1
    SKIPPED = 2
    PENDING_APPROVAL = 3
    REJECTED = 4
    MISSED = 5
    RESCHEDULED = 6

    @property
    def label(self) -> str:
        """A readable name for the numeric status."""
        return self.name.lower()





def _to_camel(name: str) -> str:
    """Convert ``snake_case`` to ``camelCase``."""
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


_camel = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


class FrequencyMetadata(BaseModel):
    """Schedule configuration for non-trivial recurrence rules."""

    model_config = _camel

    days: list[str] | None = None
    months: list[str] | None = None
    unit: str | None = None
    time: datetime | None = None
    timezone: str | None = None
    week_pattern: str | None = None
    occurrences: list[int] | None = None
    week_numbers: list[int] | None = None

    @field_validator("time", mode="before")
    @classmethod
    def _blank_time_is_unset(cls, value: Any) -> Any:
        """Donetick returns ``time: ""`` rather than omitting it."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class NotificationMetadata(BaseModel):
    """Reminder configuration for a chore."""

    model_config = _camel

    nagging: bool | None = None
    predue: bool | None = None
    templates: list[dict[str, Any]] | None = None


class Label(BaseModel):
    """A circle label.

    ``color`` is optional because a label attached to a chore only needs its
    ID; sending an empty colour would overwrite the stored one.
    """

    model_config = _camel

    id: int
    label_id: int | None = None
    name: str
    color: str | None = None
    created_by: int | None = None


class SubTask(BaseModel):
    """A checklist item belonging to a chore."""

    model_config = _camel

    id: int = 0
    order_id: int = 0
    name: str
    completed_at: datetime | None = None
    completed_by: int | None = None
    parent_id: int | None = None


class ChoreAssignees(BaseModel):
    """Reference to a user who may be assigned a chore."""

    model_config = _camel

    user_id: int


class DonetickChore(BaseModel):
    """A chore as returned by ``GET /api/v1/chores/``."""

    model_config = _camel

    id: int | None = None
    name: str
    description: str | None = None
    frequency: int = 1
    frequency_type: FrequencyType = FrequencyType.ONCE
    frequency_metadata: FrequencyMetadata | None = None
    next_due_date: datetime | None = None
    is_rolling: bool = False
    assigned_to: int | None = None
    assignees: list[ChoreAssignees] = Field(default_factory=list)
    assign_strategy: AssignmentStrategy = AssignmentStrategy.NO_ASSIGNEE
    is_active: bool = True
    notification: bool = False
    notification_metadata: NotificationMetadata | None = None
    labels: str | None = None
    labels_v2: list[Label] | None = None
    circle_id: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: int | None = None
    updated_by: int | None = None
    status: ChoreStatus = ChoreStatus.NO_STATUS
    sync_version: int | None = None
    priority: int = 0
    completion_window: int | None = None
    points: int | None = None
    require_approval: bool = False
    is_private: bool = False
    project_id: int | None = None
    sub_tasks: list[SubTask] | None = None
    thing_chore: dict[str, Any] | None = None


class ChoreReq(BaseModel):
    """Request body for ``POST /api/v1/chores/`` and ``PUT /api/v1/chores/``.

    Donetick's create and update endpoints share a single request struct, and
    the due date is always carried in ``nextDueDate``.  Earlier releases used
    ``dueDate``; that name is silently ignored now, so it must not be used.
    """

    model_config = _camel

    id: int | None = None
    name: str
    frequency_type: FrequencyType = FrequencyType.ONCE
    frequency: int = 1
    frequency_metadata: FrequencyMetadata | None = None
    next_due_date: datetime | None = None
    is_rolling: bool = False
    assignees: list[ChoreAssignees] = Field(default_factory=list)
    assigned_to: int | None = None
    assign_strategy: AssignmentStrategy = AssignmentStrategy.NO_ASSIGNEE
    is_active: bool = True
    notification: bool = False
    notification_metadata: NotificationMetadata | None = None
    labels_v2: list[Label] | None = None
    priority: int = 0
    completion_window: int | None = None
    points: int | None = None
    description: str | None = None
    sub_tasks: list[SubTask] | None = None
    require_approval: bool = False
    is_private: bool = False
    project_id: int | None = None

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, value: int) -> int:
        """Reject a zero interval, which silently breaks rescheduling."""
        if value < 1:
            msg = "frequency must be at least 1 (0 breaks recurring rescheduling)"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _require_due_date_when_rolling(self) -> ChoreReq:
        """Donetick binds ``nextDueDate`` with ``required_with=IsRolling``.

        Validated at the model level rather than on the field, because
        ``is_rolling`` is declared after ``next_due_date`` and a field
        validator could not see it.
        """
        if self.is_rolling and self.next_due_date is None:
            msg = (
                "next_due_date is required when is_rolling is true; "
                "pass due_date to set or restore it"
            )
            raise ValueError(msg)
        return self


class UserProfile(BaseModel):
    """The authenticated user's profile."""

    model_config = _camel

    id: int
    display_name: str = ""
    email: str = ""
    username: str = ""
    circle_id: int = 0


class CircleMember(BaseModel):
    """A member of the current circle, including role and points."""

    model_config = _camel

    id: int = 0
    user_id: int
    username: str = ""
    display_name: str = ""
    role: str = ""
    is_active: bool = True
    points: int = 0
    points_redeemed: int = 0


class ChoreHistoryEntry(BaseModel):
    """A single completion record."""

    model_config = _camel

    id: int = 0
    chore_id: int = 0
    performed_at: datetime | None = None
    completed_by: int | None = None
    assigned_to: int | None = None
    notes: str | None = None
    due_date: datetime | None = None
    #: Donetick sends a numeric completion status, not a label.
    status: int | None = None
    duration: int | None = None


# ---------------------------------------------------------------------------
# Tool-facing response models
# ---------------------------------------------------------------------------


class ChoreSummary(BaseModel):
    """Compact chore view used by list and search results."""

    id: int | None = None
    name: str
    due_date: str | None = None
    active: bool = True
    assigned_to: int | None = None
    assignees: list[int] = Field(default_factory=list)
    frequency_type: str = ""
    priority: int = 0
    points: int | None = None
    overdue: bool = False


class ChoreDetail(BaseModel):
    """Full chore detail returned by ``get_chore``."""

    id: int | None = None
    name: str
    description: str | None = None
    due_date: str | None = None
    active: bool = True
    assigned_to: int | None = None
    assignees: list[int] = Field(default_factory=list)
    assign_strategy: str = ""
    frequency: int = 1
    frequency_type: str = ""
    frequency_metadata: dict[str, Any] | None = None
    is_rolling: bool = False
    priority: int = 0
    points: int | None = None
    completion_window: int | None = None
    require_approval: bool = False
    is_private: bool = False
    status: int = 0
    status_label: str = ""
    labels: list[str] = Field(default_factory=list)
    sub_tasks: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class UserSummary(BaseModel):
    """Compact user view for the users resource and circle listing."""

    id: int
    display_name: str = ""
    username: str = ""
    email: str = ""
    role: str = ""
    points: int = 0


class LabelSummary(BaseModel):
    """A circle label."""

    id: int
    name: str
    color: str = ""


class SubtaskSummary(BaseModel):
    """A checklist item on a chore."""

    id: int
    name: str
    order: int = 0
    completed: bool = False
    completed_at: str | None = None


class HistorySummary(BaseModel):
    """A completion history record."""

    id: int = 0
    chore_id: int = 0
    chore_name: str = ""
    performed_at: str | None = None
    completed_by: int | None = None
    notes: str | None = None
    due_date: str | None = None
    #: Donetick's numeric completion status (1 for a normal completion).
    status: int | None = None
    status_label: str = ""


class TimerState(BaseModel):
    """Response from the chore timer endpoints.

    Donetick's ``status`` here is the *chore* status, not the time session's
    own status: ``PUT /chores/{id}/start`` reports ``in_progress`` and
    ``PUT /chores/{id}/pause`` reports ``paused``.
    """

    chore_id: int
    duration: int = 0
    chore_status: int = 0
    chore_status_label: str = ""
    timer_updated_at: str | None = None


class ErrorResponse(BaseModel):
    """Structured error payload returned instead of raising."""

    error: str


class MessageResponse(BaseModel):
    """Generic acknowledgement."""

    message: str


class ChoreCreatedResponse(BaseModel):
    """Result of ``create_chore``."""

    id: int | None = None
    name: str = ""
    message: str
    warnings: list[str] = Field(default_factory=list)


class ChoreUpdatedResponse(BaseModel):
    """Result of ``update_chore``."""

    id: int
    message: str
    warnings: list[str] = Field(default_factory=list)


class ChoreActionResponse(BaseModel):
    """Result of an action that returns the resulting chore."""

    id: int
    message: str
    chore: ChoreSummary | None = None
    rescheduled: bool = False
    next_due: str | None = None
