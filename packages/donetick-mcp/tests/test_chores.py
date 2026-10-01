"""Integration tests for chore operations against a live Donetick server.

Run with ``DONETICK_INTEGRATION=1 pytest``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import requires_integration
from donetick_mcp import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreReq,
    DonetickClient,
    FrequencyType,
    Label,
    SubTask,
)

pytestmark = requires_integration


class TestListChores:
    async def test_list_returns_a_list(self, client: DonetickClient) -> None:
        assert isinstance(await client.list_chores(), list)

    async def test_created_chore_appears_in_the_list(
        self, client: DonetickClient
    ) -> None:
        chore_id = await client.create_chore(ChoreReq(name="List Test Chore"))
        ids = [chore.id for chore in await client.list_chores()]
        assert chore_id in ids


class TestCreateChore:
    async def test_create_minimal(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Minimal Chore"))
        assert chore_id > 0

    async def test_create_with_description(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(
            ChoreReq(name="Described Chore", description="A chore with a description")
        )
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.description == "A chore with a description"

    async def test_due_date_survives_the_round_trip(self, client: DonetickClient) -> None:
        """Regression: the due date used to be sent as `dueDate` and dropped."""
        due = datetime.now(UTC) + timedelta(days=7)
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Due Date Chore",
                next_due_date=due,
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.next_due_date is not None
        assert abs((chore.next_due_date - due).total_seconds()) < 1

    async def test_assign_strategy_survives(self, client: DonetickClient) -> None:
        """Regression: an empty assignee list used to force no_assignee."""
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Rotation Chore",
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
                frequency_type=FrequencyType.DAILY,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
            )
        )
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.assign_strategy is AssignmentStrategy.KEEP_LAST_ASSIGNED

    async def test_priority_survives(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Priority Chore", priority=3))
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.priority == 3

    async def test_frequency_metadata_survives(self, client: DonetickClient) -> None:
        from donetick_mcp import build_frequency_metadata

        chore_id = await client.create_chore(
            ChoreReq(
                name="Days Of The Week Chore",
                frequency_type=FrequencyType.DAYS_OF_THE_WEEK,
                frequency=1,
                frequency_metadata=build_frequency_metadata(
                    FrequencyType.DAYS_OF_THE_WEEK,
                    days=["monday", "thursday"],
                    timezone="America/New_York",
                ),
                next_due_date=datetime.now(UTC) + timedelta(days=3),
            )
        )
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.frequency_type is FrequencyType.DAYS_OF_THE_WEEK
        assert chore.frequency_metadata is not None
        assert set(chore.frequency_metadata.days or []) >= {"monday", "thursday"}
        assert chore.frequency_metadata.timezone == "America/New_York"

    async def test_labels_survive(self, client: DonetickClient) -> None:
        label = await client.create_label(f"test-{chore_name_suffix()}", "#123456")
        try:
            chore_id = await client.create_chore(
                ChoreReq(
                    name="Labelled Chore",
                    labels_v2=[
                        {"id": label.id, "label_id": label.id, "name": label.name}  # type: ignore[list-item]
                    ],
                )
            )
            chore = await client.get_chore(chore_id)
            assert chore is not None
            assert chore.labels_v2 is not None
            assert [item.id for item in chore.labels_v2] == [label.id]
        finally:
            await client.delete_label(label.id)

    async def test_subtasks_survive(self, client: DonetickClient) -> None:
        from donetick_mcp import build_subtasks

        chore_id = await client.create_chore(
            ChoreReq(
                name="Checklist Chore",
                sub_tasks=build_subtasks(["clear counters", "scrub floor"]),
            )
        )
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.sub_tasks is not None
        assert [task.name for task in chore.sub_tasks] == [
            "clear counters",
            "scrub floor",
        ]
        assert all(task.id > 0 for task in chore.sub_tasks)


class TestGetChore:
    async def test_get_existing(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Get Test Chore"))
        chore = await client.get_chore(chore_id)
        assert chore is not None
        assert chore.id == chore_id

    async def test_get_nonexistent_returns_none(self, client: DonetickClient) -> None:
        assert await client.get_chore(999999) is None


class TestUpdateChore:
    async def _updated_req(
        self, client: DonetickClient, chore_id: int
    ) -> tuple[object, ChoreReq]:
        chore = await client.get_chore(chore_id)
        assert chore is not None
        return chore, await client.chore_to_req(chore)

    async def test_update_name(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Original Name"))
        _, req = await self._updated_req(client, chore_id)
        req.name = "Updated Name"
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.name == "Updated Name"

    async def test_update_survives_a_chore_with_no_labels_or_subtasks(
        self, client: DonetickClient
    ) -> None:
        """Regression: the server panics and drops the connection if either
        ``labelsV2`` or ``subTasks`` is missing from an edit payload.

        Donetick's edit handler dereferences both pointers with no nil check,
        so a bare chore is the exact case that used to fail.
        """
        chore_id = await client.create_chore(ChoreReq(name="Bare Chore"))
        _, req = await self._updated_req(client, chore_id)
        assert not req.labels_v2
        assert not req.sub_tasks
        req.name = "Bare Chore Renamed"

        # Would raise DonetickTransportError without the fix.
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.name == "Bare Chore Renamed"

    async def test_update_keeps_labels_and_subtasks(
        self, client: DonetickClient
    ) -> None:
        """The payload replaces everything, so untouched children must survive."""
        label = await client.create_label(f"Keep Label {chore_name_suffix()}", "#ff0000")
        chore_id = await client.create_chore(
            ChoreReq(
                name=f"Keepers {chore_name_suffix()}",
                labels_v2=[Label(id=label.id, label_id=label.id, name=label.name)],
                sub_tasks=[SubTask(name="alpha"), SubTask(name="beta")],
            )
        )
        _, req = await self._updated_req(client, chore_id)
        req.name = f"Keepers Renamed {chore_name_suffix()}"
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert [item.name for item in updated.sub_tasks or []] == ["alpha", "beta"]
        assert [item.name for item in updated.labels_v2 or []] == [label.name]

    async def test_update_description(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Desc Update Chore"))
        _, req = await self._updated_req(client, chore_id)
        req.description = "New description"
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.description == "New description"

    async def test_update_priority(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Priority Update Chore"))
        _, req = await self._updated_req(client, chore_id)
        req.priority = 4
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.priority == 4

    async def test_update_due_date_via_the_full_payload(
        self, client: DonetickClient
    ) -> None:
        chore_id = await client.create_chore(
            ChoreReq(
                name="Due Date Update Chore",
                next_due_date=datetime.now(UTC) + timedelta(days=1),
            )
        )
        _, req = await self._updated_req(client, chore_id)
        target = datetime.now(UTC) + timedelta(days=10)
        req.next_due_date = target
        await client.update_chore(req)

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.next_due_date is not None
        assert abs((updated.next_due_date - target).total_seconds()) < 1

    async def test_update_due_date_via_the_dedicated_endpoint(
        self, client: DonetickClient
    ) -> None:
        chore_id = await client.create_chore(
            ChoreReq(
                name="Quick Due Date Chore",
                next_due_date=datetime.now(UTC) + timedelta(days=1),
            )
        )
        target = datetime.now(UTC) + timedelta(days=20)
        await client.update_due_date(chore_id, target.isoformat())

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.next_due_date is not None
        assert abs((updated.next_due_date - target).total_seconds()) < 1

    async def test_add_and_remove_a_subtask(self, client: DonetickClient) -> None:
        from donetick_mcp import build_subtasks

        chore_id = await client.create_chore(
            ChoreReq(name="Subtask Chore", sub_tasks=build_subtasks(["first"]))
        )
        chore, req = await self._updated_req(client, chore_id)
        await client.replace_subtasks(
            chore,  # type: ignore[arg-type]
            [*build_subtasks(["first"]), *build_subtasks(["second"])],
        )

        updated = await client.get_chore(chore_id)
        assert updated is not None
        assert updated.sub_tasks is not None
        assert [t.name for t in updated.sub_tasks] == ["first", "second"]

        # Removing via the authoritative list deletes the row.
        await client.replace_subtasks(updated, [updated.sub_tasks[0]])  # type: ignore[arg-type]
        pruned = await client.get_chore(chore_id)
        assert pruned is not None
        assert pruned.sub_tasks is not None
        assert [t.name for t in pruned.sub_tasks] == ["first"]
        del req


class TestCompleteChore:
    async def _assigned_daily(self, client: DonetickClient, name: str) -> int:
        profile = await client.get_profile()
        return await client.create_chore(
            ChoreReq(
                name=name,
                frequency_type=FrequencyType.DAILY,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )

    async def test_one_off_chore_does_not_reschedule(
        self, client: DonetickClient
    ) -> None:
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Complete Once Chore",
                frequency_type=FrequencyType.ONCE,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )
        updated = await client.complete_chore(chore_id)
        assert updated.next_due_date is None

    async def test_daily_chore_reschedules(self, client: DonetickClient) -> None:
        due = datetime.now(UTC) + timedelta(hours=1)
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Complete Daily Chore",
                frequency_type=FrequencyType.DAILY,
                next_due_date=due,
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )
        updated = await client.complete_chore(chore_id)
        assert updated.next_due_date is not None
        assert updated.next_due_date > due

    async def test_completion_with_a_note(self, client: DonetickClient) -> None:
        chore_id = await self._assigned_daily(client, "Complete With Note Chore")
        updated = await client.complete_chore(chore_id, note="Done with care")
        assert updated.id == chore_id

    async def test_skip_moves_to_the_next_occurrence(
        self, client: DonetickClient
    ) -> None:
        due = datetime.now(UTC) + timedelta(hours=1)
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Skip Chore",
                frequency_type=FrequencyType.DAILY,
                next_due_date=due,
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )
        await client.skip_chore(chore_id, note="away for the week")
        skipped = await client.get_chore(chore_id)
        assert skipped is not None
        assert skipped.next_due_date is not None
        assert skipped.next_due_date > due


class TestApprovalFlow:
    async def test_completion_awaits_approval(self, client: DonetickClient) -> None:
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name="Approval Chore",
                frequency_type=FrequencyType.DAILY,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
                require_approval=True,
            )
        )
        await client.complete_chore(chore_id, note="done, please confirm")
        await client.approve_chore(chore_id)

        history = await client.get_chore_history(chore_id)
        assert len(history) >= 1


class TestArchiveAndDelete:
    async def test_archive_removes_from_the_default_list(
        self, client: DonetickClient
    ) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Archive Me Chore"))
        await client.archive_chore(chore_id)
        ids = [chore.id for chore in await client.list_chores()]
        assert chore_id not in ids

    async def test_archived_chore_is_listed_and_restorable(
        self, client: DonetickClient
    ) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Restore Me Chore"))
        await client.archive_chore(chore_id)
        assert chore_id in [chore.id for chore in await client.list_archived_chores()]

        await client.unarchive_chore(chore_id)
        assert chore_id in [chore.id for chore in await client.list_chores()]

    async def test_permanent_delete(self, client: DonetickClient) -> None:
        chore_id = await client.create_chore(ChoreReq(name="Delete Me Chore"))
        await client.delete_chore(chore_id)
        assert await client.get_chore(chore_id) is None


class TestTimers:
    async def _timer_chore(self, client: DonetickClient) -> int:
        profile = await client.get_profile()
        return await client.create_chore(
            ChoreReq(
                name=f"Timer Chore {chore_name_suffix()}",
                frequency_type=FrequencyType.ONCE,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )

    async def test_start_reports_an_active_timer(self, client: DonetickClient) -> None:
        chore_id = await self._timer_chore(client)
        state = await client.start_timer(chore_id)
        assert state.chore_id == chore_id
        assert state.chore_status_label == "in_progress"

    async def test_pause_reports_a_paused_timer(self, client: DonetickClient) -> None:
        chore_id = await self._timer_chore(client)
        await client.start_timer(chore_id)
        state = await client.pause_timer(chore_id)
        assert state.chore_status_label == "paused"

    async def test_starting_a_running_timer_raises(
        self, client: DonetickClient
    ) -> None:
        """Donetick answers 200 with an error body here; that must not read as success."""
        from donetick_mcp import DonetickError

        chore_id = await self._timer_chore(client)
        await client.start_timer(chore_id)
        with pytest.raises(DonetickError, match="state that can be started"):
            await client.start_timer(chore_id)


class TestLabels:
    async def test_label_crud(self, client: DonetickClient) -> None:
        suffix = chore_name_suffix()
        label = await client.create_label(f"crud-{suffix}", "#FF5733")
        assert label.id > 0

        renamed = await client.update_label(label.id, f"crud2-{suffix}", "#00FF00")
        assert renamed.name == f"crud2-{suffix}"

        await client.delete_label(label.id)
        assert label.id not in [item.id for item in await client.list_labels()]

    async def test_catalog_lowercases_names(self, client: DonetickClient) -> None:
        suffix = chore_name_suffix()
        await client.create_label(f"Catalog-{suffix}", "#123456")
        try:
            catalog = await client.get_label_catalog()
            assert f"catalog-{suffix}" in catalog
        finally:
            for label in await client.list_labels():
                if label.name.lower() == f"catalog-{suffix}":
                    await client.delete_label(label.id)


class TestHistory:
    async def test_history_records_a_completion(self, client: DonetickClient) -> None:
        profile = await client.get_profile()
        chore_id = await client.create_chore(
            ChoreReq(
                name=f"History Chore {chore_name_suffix()}",
                frequency_type=FrequencyType.DAILY,
                next_due_date=datetime.now(UTC) + timedelta(days=1),
                assignees=[ChoreAssignees(user_id=profile.id)],
                assigned_to=profile.id,
                assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            )
        )
        await client.complete_chore(chore_id, note="historic")

        history = await client.get_chore_history(chore_id)
        assert len(history) >= 1
        assert history[0].completed_by == profile.id

    async def test_circle_history_is_readable(self, client: DonetickClient) -> None:
        assert isinstance(await client.get_history(7, include_circle=True), list)


def chore_name_suffix() -> str:
    """A short unique suffix so repeated runs do not collide on unique names."""
    return f"{int(datetime.now(UTC).timestamp() * 1000) % 1_000_000:06d}"