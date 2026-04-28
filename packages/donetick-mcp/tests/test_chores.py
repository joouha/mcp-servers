"""Integration tests for chore CRUD operations via DonetickClient."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from donetick_mcp import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreReq,
    DonetickClient,
    FrequencyType,
)


class TestListChores:
    """Tests for listing chores."""

    def test_list_empty(self, client: DonetickClient) -> None:
        chores = client.list_chores()
        # May or may not be empty depending on test ordering, but should not error
        assert isinstance(chores, list)

    def test_list_returns_created_chore(self, client: DonetickClient) -> None:
        req = ChoreReq(name="List Test Chore")
        chore_id = client.create_chore(req)
        assert chore_id is not None

        chores = client.list_chores()
        ids = [c.id for c in chores]
        assert chore_id in ids


class TestCreateChore:
    """Tests for creating chores."""

    def test_create_minimal(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Minimal Chore")
        chore_id = client.create_chore(req)
        assert isinstance(chore_id, int)
        assert chore_id > 0

    def test_create_with_description(self, client: DonetickClient) -> None:
        req = ChoreReq(
            name="Described Chore",
            description="A chore with a description",
        )
        chore_id = client.create_chore(req)
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.description == "A chore with a description"

    def test_create_with_due_date(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        due = datetime.now(UTC) + timedelta(days=7)
        req = ChoreReq(
            name="Due Date Chore",
            due_date=due.isoformat(),
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.next_due_date is not None

    def test_create_with_priority(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        req = ChoreReq(
            name="Priority Chore",
            priority=3,
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.priority == 3

    def test_create_recurring_daily(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        due = datetime.now(UTC) + timedelta(days=1)
        req = ChoreReq(
            name="Daily Chore",
            frequency_type=FrequencyType.DAILY,
            due_date=due.isoformat(),
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.frequency_type == FrequencyType.DAILY


class TestGetChore:
    """Tests for retrieving a single chore."""

    def test_get_existing(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Get Test Chore")
        chore_id = client.create_chore(req)

        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.id == chore_id
        assert chore.name == "Get Test Chore"

    def test_get_nonexistent(self, client: DonetickClient) -> None:
        chore = client.get_chore(999999)
        assert chore is None


class TestUpdateChore:
    """Tests for updating chores."""

    def test_update_name(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Original Name")
        chore_id = client.create_chore(req)

        existing = client.get_chore(chore_id)
        assert existing is not None

        # Build update request from existing chore
        existing_data = existing.model_dump()
        existing_data["due_date"] = (
            existing.next_due_date.isoformat() if existing.next_due_date else ""
        )
        update_req = ChoreReq.model_validate(existing_data)
        update_req.name = "Updated Name"
        if update_req.labels_v2 is None:
            update_req.labels_v2 = []
        if update_req.description is None:
            update_req.description = ""
        if update_req.sub_tasks is None:
            update_req.sub_tasks = []

        client.update_chore(update_req)

        updated = client.get_chore(chore_id)
        assert updated is not None
        assert updated.name == "Updated Name"

    def test_update_description(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Desc Update Chore")
        chore_id = client.create_chore(req)

        existing = client.get_chore(chore_id)
        assert existing is not None

        existing_data = existing.model_dump()
        existing_data["due_date"] = (
            existing.next_due_date.isoformat() if existing.next_due_date else ""
        )
        update_req = ChoreReq.model_validate(existing_data)
        update_req.description = "New description"
        if update_req.labels_v2 is None:
            update_req.labels_v2 = []
        if update_req.sub_tasks is None:
            update_req.sub_tasks = []

        client.update_chore(update_req)

        updated = client.get_chore(chore_id)
        assert updated is not None
        assert updated.description == "New description"

    def test_update_priority(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Priority Update Chore")
        chore_id = client.create_chore(req)

        existing = client.get_chore(chore_id)
        assert existing is not None

        existing_data = existing.model_dump()
        existing_data["due_date"] = (
            existing.next_due_date.isoformat() if existing.next_due_date else ""
        )
        update_req = ChoreReq.model_validate(existing_data)
        update_req.priority = 4
        if update_req.labels_v2 is None:
            update_req.labels_v2 = []
        if update_req.description is None:
            update_req.description = ""
        if update_req.sub_tasks is None:
            update_req.sub_tasks = []

        client.update_chore(update_req)

        updated = client.get_chore(chore_id)
        assert updated is not None
        assert updated.priority == 4


class TestCompleteChore:
    """Tests for completing chores."""

    def test_complete_once_chore(self, client: DonetickClient) -> None:
        """Completing a one-time chore should not reschedule."""
        profile = client.get_profile()
        due = datetime.now(UTC) + timedelta(days=1)
        req = ChoreReq(
            name="Complete Once Chore",
            frequency_type=FrequencyType.ONCE,
            due_date=due.isoformat(),
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)

        updated = client.complete_chore(chore_id)
        assert updated is not None
        # Once chore should have no next due date after completion
        assert updated.next_due_date is None

    def test_complete_daily_chore_reschedules(self, client: DonetickClient) -> None:
        """Completing a daily chore should reschedule to the next day."""
        profile = client.get_profile()
        due = datetime.now(UTC) + timedelta(hours=1)
        req = ChoreReq(
            name="Complete Daily Chore",
            frequency_type=FrequencyType.DAILY,
            due_date=due.isoformat(),
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)

        updated = client.complete_chore(chore_id)
        assert updated is not None
        assert updated.next_due_date is not None
        # Next due date should be roughly 1 day from the original due date
        assert updated.next_due_date > due

    def test_complete_with_note(self, client: DonetickClient) -> None:
        profile = client.get_profile()
        due = datetime.now(UTC) + timedelta(days=1)
        req = ChoreReq(
            name="Complete With Note Chore",
            frequency_type=FrequencyType.ONCE,
            due_date=due.isoformat(),
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
        )
        chore_id = client.create_chore(req)

        # Should not raise
        updated = client.complete_chore(chore_id, note="Done with care")
        assert updated is not None


class TestArchiveChore:
    """Tests for archiving (deleting) chores."""

    def test_archive_chore(self, client: DonetickClient) -> None:
        req = ChoreReq(name="Archive Me Chore")
        chore_id = client.create_chore(req)

        # Should not raise
        client.archive_chore(chore_id)

        # After archiving, the chore should not appear in the default list
        chores = client.list_chores()
        ids = [c.id for c in chores]
        assert chore_id not in ids
