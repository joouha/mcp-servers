"""Integration tests for the MCP tool functions.

These tests exercise the MCP tool layer (list_chores, search_chores,
get_chore, create_chore, update_chore, complete_chore, delete_chore)
by calling the underlying client methods that the tools delegate to,
verifying the full round-trip against a live Donetick server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from donetick_mcp import (
    AssignmentStrategy,
    ChoreAssignees,
    ChoreReq,
    DonetickClient,
    FrequencyType,
    _chore_detail,
    _chore_summary,
)


class TestChoreSummaryHelper:
    """Tests for the _chore_summary helper."""

    def test_summary_fields(self, client: DonetickClient) -> None:
        due = datetime.now(UTC) + timedelta(days=3)
        req = ChoreReq(name="Summary Helper Test", due_date=due.isoformat(), priority=2)
        chore_id = client.create_chore(req)

        chore = client.get_chore(chore_id)
        assert chore is not None

        summary = _chore_summary(chore)
        assert summary.id == chore_id
        assert summary.name == "Summary Helper Test"
        assert summary.due is not None
        assert summary.priority == 2
        assert summary.active is True


class TestChoreDetailHelper:
    """Tests for the _chore_detail helper."""

    def test_detail_fields(self, client: DonetickClient) -> None:
        req = ChoreReq(
            name="Detail Helper Test",
            description="Detailed description",
            priority=1,
        )
        chore_id = client.create_chore(req)

        chore = client.get_chore(chore_id)
        assert chore is not None

        detail = _chore_detail(chore)
        assert detail.id == chore_id
        assert detail.name == "Detail Helper Test"
        assert detail.description == "Detailed description"
        assert detail.priority == 1
        assert detail.frequency_type == "once"
        assert isinstance(detail.labels, list)
        assert isinstance(detail.sub_tasks, list)


class TestSearchChores:
    """Tests for client-side search (substring matching)."""

    def test_search_by_name(self, client: DonetickClient) -> None:
        unique = f"UniqueSearchTarget-{datetime.now(UTC).timestamp()}"
        req = ChoreReq(name=unique)
        client.create_chore(req)

        chores = client.list_chores()
        q = unique.lower()
        results = [
            c
            for c in chores
            if q in c.name.lower() or (c.description and q in c.description.lower())
        ]
        assert len(results) >= 1
        assert any(c.name == unique for c in results)

    def test_search_no_match(self, client: DonetickClient) -> None:
        chores = client.list_chores()
        q = "zzz_nonexistent_chore_zzz"
        results = [
            c
            for c in chores
            if q in c.name.lower() or (c.description and q in c.description.lower())
        ]
        assert len(results) == 0


class TestEndToEndWorkflow:
    """Full workflow: create → get → update → complete → archive."""

    def test_full_lifecycle(self, client: DonetickClient) -> None:
        profile = client.get_profile()

        # 1. Create
        due = datetime.now(UTC) + timedelta(days=2)
        req = ChoreReq(
            name="Lifecycle Chore",
            description="End-to-end test",
            due_date=due.isoformat(),
            frequency_type=FrequencyType.ONCE,
            assignees=[ChoreAssignees(user_id=profile.id)],
            assigned_to=profile.id,
            assign_strategy=AssignmentStrategy.KEEP_LAST_ASSIGNED,
            priority=2,
        )
        chore_id = client.create_chore(req)
        assert chore_id is not None

        # 2. Get
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert chore.name == "Lifecycle Chore"

        # 3. Update
        existing_data = chore.model_dump()
        existing_data["due_date"] = (
            chore.next_due_date.isoformat() if chore.next_due_date else ""
        )
        update_req = ChoreReq.model_validate(existing_data)
        update_req.name = "Lifecycle Chore Updated"
        if update_req.labels_v2 is None:
            update_req.labels_v2 = []
        if update_req.description is None:
            update_req.description = ""
        if update_req.sub_tasks is None:
            update_req.sub_tasks = []
        for label in update_req.labels_v2:
            if label.label_id is None:
                label.label_id = label.id

        client.update_chore(update_req)

        updated = client.get_chore(chore_id)
        assert updated is not None
        assert updated.name == "Lifecycle Chore Updated"

        # 4. Complete
        completed = client.complete_chore(chore_id)
        assert completed is not None

        # 5. Archive
        client.archive_chore(chore_id)
        chores = client.list_chores()
        ids = [c.id for c in chores]
        assert chore_id not in ids
