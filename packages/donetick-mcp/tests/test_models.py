"""Unit tests for the request/response models.

These cover the wire-format details that are easy to get wrong and silent when
broken: field aliases, the ``nextDueDate`` rename, and frequency validation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from donetick_mcp import (
    AssignmentStrategy,
    ChoreHistoryEntry,
    ChoreHistoryStatus,
    ChoreReq,
    ChoreStatus,
    DonetickChore,
    FrequencyMetadata,
    FrequencyType,
    Label,
    SubTask,
    chore_status_label,
    history_status_label,
)

NOW = "2026-12-25T09:00:00Z"


class TestChoreReqSerialisation:
    """The request model must emit exactly what Donetick binds."""

    def test_due_date_is_sent_as_next_due_date(self) -> None:
        """Regression: `dueDate` is silently ignored by current Donetick."""
        due = datetime(2026, 12, 25, 9, 0, tzinfo=UTC)
        payload = ChoreReq(name="x", next_due_date=due).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        assert "nextDueDate" in payload
        assert "dueDate" not in payload

    def test_round_trips_a_server_payload(self) -> None:
        raw = {
            "id": 7,
            "name": "Mow lawn",
            "nextDueDate": "2026-12-25T09:00:00Z",
            "frequencyType": "weekly",
            "frequency": 2,
            "isRolling": True,
            "assignedTo": 3,
            "assignees": [{"userId": 3}, {"userId": 4}],
            "assignStrategy": "round_robin",
            "subTasks": [{"id": 1, "orderId": 0, "name": "front"}],
            "labelsV2": [{"id": 2, "name": "outdoor", "color": "#0f0"}],
        }
        req = ChoreReq.model_validate(raw)
        assert req.next_due_date == datetime(2026, 12, 25, 9, 0, tzinfo=UTC)
        assert req.frequency_type is FrequencyType.WEEKLY
        assert req.assign_strategy is AssignmentStrategy.ROUND_ROBIN
        assert [a.user_id for a in req.assignees] == [3, 4]

        dumped = req.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert dumped["subTasks"][0]["orderId"] == 0
        assert dumped["labelsV2"][0]["name"] == "outdoor"

    def test_camel_case_alias_generator(self) -> None:
        req = ChoreReq(
            name="x",
            next_due_date=datetime(2026, 1, 1, tzinfo=UTC),
            is_rolling=True,
            completion_window=60,
        )
        dumped = req.model_dump(mode="json", by_alias=True)
        assert dumped["isRolling"] is True
        assert dumped["completionWindow"] == 60
        assert dumped["assignStrategy"] == "no_assignee"
        assert dumped["frequencyType"] == "once"


class TestFrequencyValidation:
    """`frequency=0` is accepted by the server but breaks rescheduling."""

    def test_zero_frequency_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="frequency must be at least 1"):
            ChoreReq(name="x", frequency=0)

    def test_negative_frequency_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="frequency must be at least 1"):
            ChoreReq(name="x", frequency=-3)

    def test_default_frequency_is_one(self) -> None:
        assert ChoreReq(name="x").frequency == 1


class TestRollingValidation:
    """Donetick binds `nextDueDate` with `required_with=IsRolling`."""

    def test_rolling_without_due_date_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="next_due_date is required"):
            ChoreReq(name="x", is_rolling=True)

    def test_rolling_with_due_date_is_accepted(self) -> None:
        req = ChoreReq(
            name="x", is_rolling=True, next_due_date=datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert req.is_rolling is True

    def test_non_rolling_without_due_date_is_accepted(self) -> None:
        assert ChoreReq(name="x").next_due_date is None


class TestDonetickChoreParsing:
    """Server responses must parse without dropping fields."""

    def test_parses_full_server_response(self) -> None:
        raw = {
            "id": 18,
            "name": "Kitchen overhaul",
            "frequency": 1,
            "frequencyType": "days_of_the_week",
            "frequencyMetadata": {
                "days": ["monday", "thursday"],
                "unit": "days",
                "weekPattern": "every_week",
                "timezone": "America/New_York",
            },
            "nextDueDate": "2026-12-07T19:00:00Z",
            "isRolling": False,
            "assignedTo": 1,
            "assignees": [{"userId": 1}],
            "assignStrategy": "keep_last_assigned",
            "isActive": True,
            "notification": True,
            "notificationMetadata": {
                "nagging": True,
                "templates": [{"value": 15, "unit": "m"}],
            },
            "labelsV2": [{"id": 1, "name": "cleaning", "color": "#FF5733"}],
            "circleId": 1,
            "createdAt": "2026-10-01T14:32:03.529773Z",
            "updatedAt": "2026-10-01T14:32:03.529773Z",
            "status": 0,
            "priority": 2,
            "points": 25,
            "completionWindow": 3600,
            "requireApproval": True,
            "isPrivate": False,
            "syncVersion": 4,
            "subTasks": [
                {
                    "id": 1,
                    "orderId": 0,
                    "name": "clear counters",
                    "completedAt": None,
                    "completedBy": 0,
                    "parentId": None,
                }
            ],
        }
        chore = DonetickChore.model_validate(raw)
        assert chore.id == 18
        assert chore.frequency_type is FrequencyType.DAYS_OF_THE_WEEK
        assert chore.frequency_metadata is not None
        assert chore.frequency_metadata.days == ["monday", "thursday"]
        assert chore.require_approval is True
        assert chore.sync_version == 4
        assert chore.sub_tasks is not None
        assert chore.sub_tasks[0].name == "clear counters"

    def test_missing_optional_fields_default_safely(self) -> None:
        chore = DonetickChore.model_validate({"id": 1, "name": "Bare"})
        assert chore.assignees == []
        assert chore.labels_v2 is None
        assert chore.sub_tasks is None
        assert chore.frequency == 1
        assert chore.is_active is True


class TestNestedModels:
    """Small models used in payloads."""

    def test_frequency_metadata_defaults_timezone_to_none(self) -> None:
        metadata = FrequencyMetadata()
        assert metadata.timezone is None

    def test_subtask_survives_partial_payload(self) -> None:
        task = SubTask.model_validate({"orderId": 2, "name": "mop"})
        assert task.id == 0
        assert task.completed_at is None

    def test_label_requires_name(self) -> None:
        with pytest.raises(ValidationError):
            Label.model_validate({"id": 1})

    def test_label_color_is_optional(self) -> None:
        """A label attached to a chore only needs its ID; sending '' would wipe it."""
        label = Label.model_validate({"id": 1, "labelId": 1, "name": "outdoor"})
        dumped = label.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "color" not in dumped


class TestServerQuirks:
    """Shapes a live Donetick instance actually returns.

    Each case here was observed against Donetick v0.1.79, not inferred.
    """

    def test_blank_frequency_metadata_time_reads_as_unset(self) -> None:
        """Donetick sends `"time": ""` rather than omitting the key."""
        chore = DonetickChore.model_validate(
            {
                "id": 1,
                "name": "x",
                "frequencyMetadata": {
                    "days": ["monday"],
                    "unit": "days",
                    "time": "",
                    "timezone": "UTC",
                    "weekPattern": "every_week",
                },
            }
        )
        assert chore.frequency_metadata is not None
        assert chore.frequency_metadata.time is None

    def test_pending_approval_status_is_known(self) -> None:
        """`status: 3` appears once a completion awaits approval."""
        chore = DonetickChore.model_validate({"id": 1, "name": "x", "status": 3})
        assert chore.status is ChoreStatus.PENDING_APPROVAL

    def test_history_status_is_numeric(self) -> None:
        entry = ChoreHistoryEntry.model_validate(
            {"id": 9, "choreId": 31, "performedAt": NOW, "completedBy": 1, "status": 1}
        )
        assert entry.status == 1

    def test_history_status_covers_the_full_range(self) -> None:
        values = {member.value for member in ChoreHistoryStatus}
        assert values == {0, 1, 2, 3, 4, 5, 6}

    def test_history_status_labels(self) -> None:
        assert history_status_label(1) == "completed"
        assert history_status_label(2) == "skipped"
        assert history_status_label(None) == ""
        assert history_status_label(99) == "unknown_99"

    def test_chore_status_labels(self) -> None:
        assert chore_status_label(0) == "no_status"
        assert chore_status_label(ChoreStatus.PAUSED) == "paused"
        assert chore_status_label(42) == "unknown_42"