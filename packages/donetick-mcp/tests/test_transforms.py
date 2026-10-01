"""Unit tests for the argument-to-payload transforms."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from donetick_mcp import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreReq,
    DonetickChore,
    FrequencyMetadata,
    FrequencyType,
    Label,
    SubTask,
    build_frequency_metadata,
    build_notification_metadata,
    build_subtasks,
    detail_of,
    ensure_assignee_consistency,
    labels_by_name,
    normalise_days,
    normalise_months,
    parse_due_date,
    resolve_timezone,
    summary_of,
)
from donetick_mcp.transforms import iso


class TestResolveTimezone:
    def test_known_zone(self) -> None:
        assert resolve_timezone("America/New_York") == ZoneInfo("America/New_York")

    def test_unknown_zone_falls_back_to_utc(self) -> None:
        """A bad timezone degrades rather than failing every call."""
        assert resolve_timezone("Mars/Olympus") == ZoneInfo("UTC")

    def test_empty_name_falls_back_to_utc(self) -> None:
        assert resolve_timezone("") == ZoneInfo("UTC")


class TestParseDueDate:
    def test_bare_date_is_local_midnight(self) -> None:
        tz = ZoneInfo("America/New_York")
        parsed = parse_due_date("2026-12-25", tz)
        assert parsed == datetime(2026, 12, 25, 0, 0, tzinfo=tz)

    def test_bare_time_rolls_to_tomorrow_when_past(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        past = (now - timedelta(hours=1)).strftime("%H:%M")
        parsed = parse_due_date(past, tz)
        assert parsed is not None
        assert parsed.date() > now.date()

    def test_bare_time_stays_today_when_future(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        future = (now + timedelta(hours=2)).strftime("%H:%M")
        parsed = parse_due_date(future, tz)
        assert parsed is not None
        assert parsed.date() == now.date()

    def test_iso_with_z_is_converted_to_configured_zone(self) -> None:
        parsed = parse_due_date("2026-12-25T09:00:00Z", ZoneInfo("America/New_York"))
        assert parsed is not None
        assert parsed.tzinfo == ZoneInfo("America/New_York")
        assert parsed.hour == 4  # 09:00 UTC is 04:00 in December EST

    def test_naive_datetime_is_read_in_configured_zone(self) -> None:
        tz = ZoneInfo("Europe/Berlin")
        parsed = parse_due_date("2026-12-25T09:00:00", tz)
        assert parsed == datetime(2026, 12, 25, 9, 0, tzinfo=tz)

    def test_empty_string_returns_none(self) -> None:
        assert parse_due_date("   ", ZoneInfo("UTC")) is None

    def test_garbage_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_due_date("not-a-date", ZoneInfo("UTC"))


class TestNormaliseDays:
    def test_abbreviations_expand(self) -> None:
        assert normalise_days(["mon", "tue", "thurs"]) == [
            "monday",
            "tuesday",
            "thursday",
        ]

    def test_case_insensitive(self) -> None:
        assert normalise_days(["MONDAY", "Sun"]) == ["monday", "sunday"]

    def test_duplicates_collapse_preserving_order(self) -> None:
        assert normalise_days(["mon", "monday", "tue"]) == ["monday", "tuesday"]

    def test_invalid_day_names_the_valid_options(self) -> None:
        with pytest.raises(ValueError, match="Invalid day name"):
            normalise_days(["funday"])

    def test_whitespace_is_stripped(self) -> None:
        assert normalise_days(["  wed  "]) == ["wednesday"]


class TestNormaliseMonths:
    def test_abbreviations_expand(self) -> None:
        assert normalise_months(["jan", "sept", "dec"]) == [
            "january",
            "september",
            "december",
        ]

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid month name"):
            normalise_months(["smarch"])


class TestBuildFrequencyMetadata:
    def test_returns_none_when_nothing_requested(self) -> None:
        assert build_frequency_metadata(FrequencyType.ONCE) is None

    def test_days_of_the_week_requires_days(self) -> None:
        with pytest.raises(ValueError, match="days_of_week is required"):
            build_frequency_metadata(FrequencyType.DAYS_OF_THE_WEEK)

    def test_day_of_the_month_requires_months(self) -> None:
        with pytest.raises(ValueError, match="months is required"):
            build_frequency_metadata(FrequencyType.DAY_OF_THE_MONTH)

    def test_populates_scheduler_fields_for_days_of_the_week(self) -> None:
        metadata = build_frequency_metadata(
            FrequencyType.DAYS_OF_THE_WEEK,
            days=["mon", "thu"],
            timezone="America/New_York",
        )
        assert metadata is not None
        assert metadata.days == ["monday", "thursday"]
        assert metadata.timezone == "America/New_York"
        assert metadata.week_pattern == "every_week"
        assert metadata.occurrences == []
        assert metadata.week_numbers == []
        assert metadata.unit == "days"

    def test_time_of_day_is_localised(self) -> None:
        metadata = build_frequency_metadata(
            FrequencyType.DAYS_OF_THE_WEEK,
            days=["mon"],
            time_of_day="19:30",
            timezone="Europe/Berlin",
        )
        assert metadata is not None
        assert metadata.time is not None
        assert metadata.time.hour == 19
        assert metadata.time.minute == 30

    def test_months_are_normalised(self) -> None:
        metadata = build_frequency_metadata(
            FrequencyType.DAY_OF_THE_MONTH, months=["jan", "jun"]
        )
        assert metadata is not None
        assert metadata.months == ["january", "june"]


class TestBuildNotificationMetadata:
    def test_none_when_no_reminders_requested(self) -> None:
        assert build_notification_metadata() is None

    def test_minutes_before_creates_template(self) -> None:
        metadata = build_notification_metadata(remind_minutes_before=15)
        assert metadata is not None
        assert metadata.templates == [{"value": 15, "unit": "m"}]
        assert metadata.predue is True

    def test_at_due_time_uses_zero_minutes(self) -> None:
        metadata = build_notification_metadata(remind_at_due_time=True)
        assert metadata is not None
        assert metadata.templates == [{"value": 0, "unit": "m"}]

    def test_nagging_alone_still_returns_metadata(self) -> None:
        metadata = build_notification_metadata(nagging=True)
        assert metadata is not None
        assert metadata.nagging is True


class TestBuildSubtasks:
    def test_order_ids_are_sequential(self) -> None:
        tasks = build_subtasks(["a", "b", "c"])
        assert [t.name for t in tasks] == ["a", "b", "c"]
        assert [t.order_id for t in tasks] == [0, 1, 2]
        assert all(t.completed_at is None for t in tasks)

    def test_empty_list(self) -> None:
        assert build_subtasks([]) == []


class TestEnsureAssigneeConsistency:
    def test_assigned_to_is_added_to_assignees(self) -> None:
        """Donetick returns 400 when `assignedTo` is absent from `assignees`."""
        req = ChoreReq(
            name="x", assigned_to=7, assignees=[ChoreAssignees(user_id=3)]
        )
        ensure_assignee_consistency(req)
        assert [a.user_id for a in req.assignees] == [3, 7]

    def test_existing_assignment_is_not_duplicated(self) -> None:
        req = ChoreReq(
            name="x",
            assigned_to=3,
            assignees=[ChoreAssignees(user_id=3)],
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        ensure_assignee_consistency(req)
        assert [a.user_id for a in req.assignees] == [3]
        assert req.assign_strategy is AssignmentStrategy.KEEP_LAST_ASSIGNED

    def test_rotation_strategy_downgrades_when_no_assignees(self) -> None:
        """An empty assignee list cannot rotate; fall back rather than 400."""
        req = ChoreReq(name="x", assign_strategy=AssignmentStrategy.ROUND_ROBIN)
        ensure_assignee_consistency(req)
        assert req.assign_strategy is AssignmentStrategy.NO_ASSIGNEE

    def test_assignees_without_assigned_to_keep_strategy(self) -> None:
        req = ChoreReq(
            name="x",
            assignees=[ChoreAssignees(user_id=1)],
            assign_strategy=AssignmentStrategy.ROUND_ROBIN,
        )
        ensure_assignee_consistency(req)
        assert req.assign_strategy is AssignmentStrategy.ROUND_ROBIN


class TestLabelsByName:
    CATALOG = {"cleaning": 1, "outdoor": 2}

    def test_add_resolves_names_to_ids(self) -> None:
        labels = labels_by_name(None, add=["outdoor"], catalog=self.CATALOG)
        assert labels is not None
        assert [label.id for label in labels] == [2]
        assert labels[0].label_id == 2

    def test_add_unknown_label_is_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown label"):
            labels_by_name(None, add=["nonexistent"], catalog=self.CATALOG)

    def test_add_is_case_insensitive(self) -> None:
        labels = labels_by_name(None, add=["CLEANING"], catalog=self.CATALOG)
        assert labels is not None
        assert labels[0].id == 1

    def test_remove_drops_an_existing_label(self) -> None:
        existing = DonetickChore.model_validate(
            {"id": 1, "name": "x", "labelsV2": [{"id": 1, "name": "cleaning"}]}
        )
        labels = labels_by_name(existing, remove=["cleaning"], catalog=self.CATALOG)
        assert labels == []

    def test_set_replaces_everything(self) -> None:
        existing = DonetickChore.model_validate(
            {"id": 1, "name": "x", "labelsV2": [{"id": 1, "name": "cleaning"}]}
        )
        labels = labels_by_name(existing, set_names=["outdoor"], catalog=self.CATALOG)
        assert labels is not None
        assert [label.id for label in labels] == [2]

    def test_no_operations_returns_none(self) -> None:
        assert labels_by_name(None, catalog=self.CATALOG) is None

    def test_labels_with_zero_id_are_dropped(self) -> None:
        """Donetick binds label ids with `gt=0`; a 0 entry is a 400."""
        existing = DonetickChore.model_validate(
            {
                "id": 1,
                "name": "x",
                "labelsV2": [{"id": 0, "name": "ghost"}, {"id": 1, "name": "cleaning"}],
            }
        )
        labels = labels_by_name(existing, add=["outdoor"], catalog=self.CATALOG)
        assert labels is not None
        assert all(label.id > 0 for label in labels)


class TestSummaryAndDetail:
    def _chore(self, **overrides: object) -> DonetickChore:
        raw: dict[str, object] = {
            "id": 5,
            "name": "Mow lawn",
            "nextDueDate": "2026-12-25T09:00:00Z",
            "assignees": [{"userId": 3}, {"userId": 4}],
            "labelsV2": [{"id": 1, "name": "outdoor", "color": "#0f0"}],
            "subTasks": [
                {"id": 1, "orderId": 0, "name": "front", "completedAt": None},
                {
                    "id": 2,
                    "orderId": 1,
                    "name": "back",
                    "completedAt": "2026-12-01T00:00:00Z",
                },
            ],
        }
        raw.update(overrides)
        return DonetickChore.model_validate(raw)

    def test_summary_flattens_assignee_ids(self) -> None:
        summary = summary_of(self._chore())
        assert summary.assignees == [3, 4]
        assert summary.due_date == "2026-12-25T09:00:00+00:00"

    def test_summary_flags_overdue_recurring_chore(self) -> None:
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        chore = self._chore(
            nextDueDate=past, frequencyType="daily", isActive=True
        )
        assert summary_of(chore).overdue is True

    def test_summary_does_not_flag_overdue_one_off_chore(self) -> None:
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        chore = self._chore(nextDueDate=past, frequencyType="once", isActive=True)
        assert summary_of(chore).overdue is False

    def test_summary_does_not_flag_inactive_chore(self) -> None:
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        chore = self._chore(
            nextDueDate=past, frequencyType="daily", isActive=False
        )
        assert summary_of(chore).overdue is False

    def test_summary_handles_missing_due_date(self) -> None:
        assert summary_of(self._chore(nextDueDate=None)).due_date is None

    def test_detail_lists_label_names(self) -> None:
        assert detail_of(self._chore()).labels == ["outdoor"]

    def test_detail_marks_subtask_completion(self) -> None:
        subtasks = detail_of(self._chore()).sub_tasks
        assert subtasks[0]["completed"] is False
        assert subtasks[1]["completed"] is True
        assert subtasks[1]["completed_at"] is not None

    def test_detail_tolerates_missing_optional_collections(self) -> None:
        chore = self._chore(labelsV2=None, subTasks=None)
        detail = detail_of(chore)
        assert detail.labels == []
        assert detail.sub_tasks == []

    def test_detail_serialises_frequency_metadata_with_aliases(self) -> None:
        chore = self._chore(
            frequencyType="days_of_the_week",
            frequencyMetadata={"days": ["monday"], "weekPattern": "every_week"},
        )
        metadata = detail_of(chore).frequency_metadata
        assert metadata is not None
        assert metadata["weekPattern"] == "every_week"
        assert metadata["days"] == ["monday"]


class TestIso:
    def test_none_passes_through(self) -> None:
        assert iso(None) is None

    def test_datetime_is_rendered(self) -> None:
        moment = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        assert iso(moment) == "2026-01-02T03:04:00+00:00"


class TestUnreferencedHelpers:
    """Guard the small helpers that have no direct test above."""

    def test_labels_for_update_drops_zero_ids(self) -> None:
        from donetick_mcp.transforms import labels_for_update

        chore = DonetickChore.model_validate(
            {
                "id": 1,
                "name": "x",
                "labelsV2": [{"id": 0, "name": "ghost"}, {"id": 4, "name": "real"}],
            }
        )
        labels = labels_for_update(chore)
        assert labels is not None
        assert [label.id for label in labels] == [4]
        assert labels[0].label_id == 4

    def test_labels_for_update_returns_none_when_absent(self) -> None:
        from donetick_mcp.transforms import labels_for_update

        chore = DonetickChore.model_validate({"id": 1, "name": "x"})
        assert labels_for_update(chore) is None

    def test_user_summary_of_flattens_member(self) -> None:
        from donetick_mcp import CircleMember
        from donetick_mcp.transforms import user_summary_of

        member = CircleMember.model_validate(
            {"userId": 9, "username": "sam", "displayName": "Sam", "role": "admin", "points": 12}
        )
        assert user_summary_of(member) == {
            "id": 9,
            "display_name": "Sam",
            "username": "sam",
            "role": "admin",
            "points": 12,
        }


class TestFrequencyMetadataModel:
    def test_aliases_cover_scheduler_fields(self) -> None:
        metadata = FrequencyMetadata.model_validate(
            {"weekNumbers": [1, 3], "occurrences": [2], "unit": "weeks"}
        )
        assert metadata.week_numbers == [1, 3]
        assert metadata.occurrences == [2]
        dumped = metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "weekNumbers" in dumped

    def test_subtask_accepts_completion_timestamp(self) -> None:
        task = SubTask.model_validate(
            {"id": 1, "orderId": 0, "name": "done", "completedAt": "2026-12-01T00:00:00Z"}
        )
        assert task.completed_at is not None

    def test_label_keeps_color(self) -> None:
        label = Label.model_validate({"id": 1, "name": "x", "color": "#abc"})
        assert label.color == "#abc"