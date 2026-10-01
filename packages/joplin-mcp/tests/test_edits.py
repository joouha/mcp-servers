"""Tests for the edit tools' safeguards, against a fake Joplin Server.

The fake speaks just enough of the REST API -- sessions, ``items`` listing and
content GET/PUT/DELETE -- to exercise the real JoplinClient end to end without a
Joplin Server running, including the partial-edit tools and their refusals.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from joplin_mcp.client import (  # noqa: E402
    JoplinClient,
    _folder_template,
    _note_tag_template,
    _note_template,
    _parse_joplin_item,
    _tag_template,
)
from joplin_mcp.models import JoplinError  # noqa: E402
from joplin_mcp.todos import now_iso  # noqa: E402

NB = "a" * 32
NB2 = "b" * 32
TAG = "c" * 32
NOTE = "d" * 32
RESOURCE = "e" * 32

#: A due date far enough ahead that it never reads as OVERDUE, which depends on
#: the real clock.
DUE = "2099-01-15"

RESOURCE_PREFIX = "/api/items/root:/.resource/"


def _resource_id_from(path: str) -> str:
    """Pull the resource ID out of a ``.resource/<id>[:/x]`` API path."""
    return path[len(RESOURCE_PREFIX) :].split(".md")[0].split(":/")[0]


LOG_BODY = """# Work Log

Notes as they happen.

## 2026-08-25

Shipped the release.

### Details

- one
- two

## 2026-08-26

Deployed.
"""

RESOURCE_ITEM = f"""photo.png

id: {RESOURCE}
mime: image/png
size: 12
file_extension: .png
created_time: 2026-01-01T00:00:00.000Z
updated_time: 2026-01-01T00:00:00.000Z
type_: 9"""


class FakeJoplin:
    """A minimal in-memory Joplin Server for the REST calls the client makes."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self.puts: list[str] = []
        self.deletes: list[str] = []
        self.resource_bytes: dict[str, bytes] = {RESOURCE: b"\x89PNG-fake-bytes"}
        self.resource_items: dict[str, str] = {RESOURCE: RESOURCE_ITEM}
        self.session = "session-1"
        self._clock = 0

    # -- fixtures -----------------------------------------------------------

    def seed(self) -> None:
        now = now_iso()
        self._store(NB, _folder_template(NB, "Work", "", now))
        self._store(NB2, _folder_template(NB2, "Archive", "", now))
        self._store(
            NOTE,
            _note_template(NOTE, "Work Log", LOG_BODY, NB, now),
        )
        self._store(TAG, _tag_template(TAG, "ops", now))
        self._store(
            "f" * 32,
            _note_tag_template("f" * 32, NOTE, TAG, "ops", now),
        )
        self.etags[RESOURCE] = "2026-01-01T00:00:01.000Z"

    def _store(self, item_id: str, content: str) -> None:
        self.items[item_id] = content
        self._clock += 1
        self.etags[item_id] = f"2026-01-01T00:00:{self._clock:02d}.000Z"

    def body(self, note_id: str) -> str:
        """The current body of a stored note."""
        return _parse_joplin_item(self.items[note_id])["body"]

    def metadata(self, item_id: str) -> dict[str, str]:
        """The current metadata block of a stored item."""
        return _parse_joplin_item(self.items[item_id])["metadata"]

    # -- transport ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/api/sessions":
            return httpx.Response(200, json={"id": self.session})

        if path == "/api/items/root:/:/children":
            items = [
                {
                    "name": f"{item_id}.md",
                    "updated_time": self.etags.get(item_id, ""),
                }
                for item_id in sorted(self.items)
            ]
            # Real Joplin lists resource metadata alongside the items, under a
            # dotted name -- that is the only place the client learns of them.
            items.extend(
                {
                    "name": f".resource/{resource_id}.md",
                    "updated_time": self.etags.get(resource_id, ""),
                }
                for resource_id in sorted(self.resource_bytes)
            )
            return httpx.Response(200, json={"items": items, "has_more": False})

        # Resource metadata first: its path also ends in ":/content", so it must
        # be matched before the binary branch below.
        if path.startswith(RESOURCE_PREFIX) and path.endswith(".md:/content"):
            resource_id = _resource_id_from(path)
            if resource_id not in self.resource_bytes:
                return httpx.Response(404)
            self.etags[resource_id] = f"{resource_id}-bumped"
            return httpx.Response(200, text=self.resource_items[resource_id])

        if path.startswith(RESOURCE_PREFIX) and path.endswith(":/content"):
            resource_id = _resource_id_from(path)
            data = self.resource_bytes.get(resource_id)
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data)

        if "/.md:" in path:
            item_id = path.split("/api/items/root:/")[1].split(".md")[0]
            if item_id in self.items:
                self.etags[item_id] = f"{item_id}-bumped"

        if method == "GET" and path.endswith(":/content"):
            item_id = path.split("/api/items/root:/")[1].split(".md")[0]
            content = self.items.get(item_id)
            if content is None:
                return httpx.Response(404)
            return httpx.Response(200, text=content)

        if method == "PUT" and path.endswith(":/content"):
            item_id = path.split("/api/items/root:/")[1].split(".md")[0]
            self.items[item_id] = request.content.decode("utf-8")
            self._clock += 1
            self.etags[item_id] = f"2026-02-01T00:00:{self._clock:02d}.000Z"
            self.puts.append(item_id)
            return httpx.Response(200)

        if method == "DELETE":
            item_id = path.split("/api/items/root:/")[1].split(".md")[0]
            if item_id not in self.items:
                return httpx.Response(404)
            del self.items[item_id]
            self.deletes.append(item_id)
            return httpx.Response(200)

        return httpx.Response(404)


@pytest.fixture(autouse=True)
def no_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the on-disk index during tests."""
    monkeypatch.setenv("JOPLIN_INDEX_CACHE_FILE", "")


@pytest.fixture
def server() -> FakeJoplin:
    fake = FakeJoplin()
    fake.seed()
    return fake


def _make_client(server: FakeJoplin, **kwargs: Any) -> JoplinClient:
    """A client wired to the fake server's transport."""
    fake_client = JoplinClient(
        url="https://joplin.test", email="u@example.com", password="pw", **kwargs
    )
    fake_client._http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return fake_client


@pytest.fixture
def client(server: FakeJoplin) -> JoplinClient:
    """A client for the fake server; no disk index is touched."""
    return _make_client(server)


# ---------------------------------------------------------------------------
# Connectivity & reads
# ---------------------------------------------------------------------------


class TestReadTools:
    def test_ping_reports_index_contents(self, client: JoplinClient) -> None:
        result = asyncio.run(client.ping())
        assert result.ok is True
        assert result.details["notes"] == 1
        assert result.details["notebooks"] == 2

    def test_get_note_substitutes_resource_labels_by_default(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "With image", f"see (:/{RESOURCE})", NB, now_iso()),
        )
        result = asyncio.run(client.get_note(NOTE))
        assert not isinstance(result, JoplinError)
        assert "photo.png" in result.body
        assert f":/{RESOURCE}" not in result.body
        assert result.resource_refs[0].title == "photo.png"

    def test_raw_note_keeps_resource_links_verbatim(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "With image", f"see (:/{RESOURCE})", NB, now_iso()),
        )
        result = asyncio.run(client.get_note(NOTE, raw=True))
        assert result.body == f"see (:/{RESOURCE})"
        assert result.raw is True

    def test_outline_lists_headings_with_line_numbers(
        self, client: JoplinClient
    ) -> None:
        result = asyncio.run(client.get_note_outline(NOTE))
        titles = [h.title for h in result.headings]
        assert titles == ["Work Log", "2026-08-25", "Details", "2026-08-26"]
        # 1-based line numbers, so they can be pasted straight into an editor.
        assert [h.line for h in result.headings] == [1, 5, 9, 14]
        assert result.body_chars > 0

    def test_get_notes_batch_reads_in_parallel(self, client: JoplinClient) -> None:
        result = asyncio.run(client.get_notes_batch([NOTE, "9" * 32]))
        assert len(result.notes) == 1
        assert result.missing == ["9" * 32]

    def test_get_notes_batch_rejects_too_many(self, client: JoplinClient) -> None:
        result = asyncio.run(client.get_notes_batch([NOTE] * 51))
        assert isinstance(result, JoplinError)
        assert "maximum is 50" in result.error

    def test_get_notes_batch_rejects_invalid_id(self, client: JoplinClient) -> None:
        result = asyncio.run(client.get_notes_batch([NOTE, "nope"]))
        assert isinstance(result, JoplinError)
        assert "32-character hex" in result.error

    def test_invalid_note_id_never_reaches_the_api(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.get_note("../../etc/passwd"))
        assert isinstance(result, JoplinError)
        assert "32-character hex" in result.error

    def test_search_requires_all_terms(self, client: JoplinClient) -> None:
        assert asyncio.run(client.search_notes("work deployed"))
        assert asyncio.run(client.search_notes("work nonexistent")) == []

    def test_search_scope_title_only(self, client: JoplinClient) -> None:
        assert asyncio.run(client.search_notes("deployed", scope="title")) == []

    def test_get_all_notes_sorts_and_pages(self, client: JoplinClient) -> None:
        result = asyncio.run(client.get_all_notes(order_by="title", limit=1))
        assert result.total == 1
        assert result.total_pages == 1

    def test_get_all_notes_rejects_bad_order(self, client: JoplinClient) -> None:
        result = asyncio.run(client.get_all_notes(order_by="body"))
        assert isinstance(result, JoplinError)
        assert "order_by" in result.error


# ---------------------------------------------------------------------------
# append_to_note
# ---------------------------------------------------------------------------


class TestAppendToNote:
    def test_appends_to_end_of_note(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.append_to_note(NOTE, "new line"))
        assert result.changed is True
        assert server.body(NOTE).endswith("Deployed.\n\nnew line")

    def test_report_gives_deltas_and_context(self, client: JoplinClient) -> None:
        result = asyncio.run(client.append_to_note(NOTE, "new line"))
        assert result.chars_after > result.chars_before
        assert result.lines_after == result.lines_before + 2
        assert result.headings_after == result.headings_before
        assert any("new line" in line for line in result.context_lines)
        assert "Chars" in result.message

    def test_report_line_number_is_one_based(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        body = "# Runbook\n\nIntro.\n\n## Hosts\n\n- host-01\n\n## Notes\n\nTail."
        server._store(NOTE, _note_template(NOTE, "Runbook", body, NB, now_iso()))
        result = asyncio.run(
            client.append_to_note(NOTE, "- host-02", section="Hosts", separator="\n")
        )
        assert result.detail == (
            "Inserted 9 chars at the end of section '## Hosts' (line 5)"
        )
        # The report points at the line the change landed on, 1-based, and
        # quotes it back with four lines of context either side.
        assert result.line == 8
        assert result.context_lines == [
            " 4 | ",
            " 5 | ## Hosts",
            " 6 | ",
            " 7 | - host-01",
            " 8 | - host-02",
            " 9 | ",
            "10 | ## Notes",
            "11 | ",
            "12 | Tail.",
        ]

    def test_dry_run_writes_nothing(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.items[NOTE]
        result = asyncio.run(client.append_to_note(NOTE, "new line", dry_run=True))
        assert result.dry_run is True
        assert result.changed is True
        assert "Would" in result.message
        assert server.items[NOTE] == before

    def test_appends_into_section(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(
            client.append_to_note(NOTE, "- three", section="Details", separator="\n")
        )
        body = server.body(NOTE)
        assert "- two\n- three" in body
        # The section's boundaries held: nothing leaked into the next section.
        assert body.index("- three") < body.index("## 2026-08-26")

    def test_before_position_lands_above_the_heading(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(
            client.append_to_note(
                NOTE,
                "## 2026-08-27\n\nDeployed.",
                section="2026-08-26",
                position="before",
            )
        )
        body = server.body(NOTE)
        assert body.index("## 2026-08-27") < body.index("## 2026-08-26")

    def test_after_position_passes_subsections(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(
            client.append_to_note(
                NOTE, "## Follow-up\n\nx", section="2026-08-25", position="after"
            )
        )
        body = server.body(NOTE)
        # Past the H3 "Details" subsection, not just its own lines.
        assert body.index("## Follow-up") > body.index("- two")
        assert body.index("## Follow-up") < body.index("## 2026-08-26")

    def test_before_and_after_rewrite_nothing_existing(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.body(NOTE)
        asyncio.run(
            client.append_to_note(NOTE, "new", section="2026-08-26", position="before")
        )
        after = server.body(NOTE)
        # Every original line survives, in order.
        original_lines = [line for line in before.split("\n") if line.strip()]
        new_lines = [line for line in after.split("\n") if line.strip()]
        assert [line for line in new_lines if line in original_lines] == original_lines

    def test_if_absent_skips_a_re_run(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        first = asyncio.run(client.append_to_note(NOTE, "- three", if_absent="- three"))
        assert first.changed is True
        second = asyncio.run(
            client.append_to_note(NOTE, "- three", if_absent="- three")
        )
        assert second.changed is False
        assert second.skipped is True
        assert server.body(NOTE).count("- three") == 1

    def test_before_without_section_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.append_to_note(NOTE, "x", position="before"))
        assert isinstance(result, JoplinError)
        assert "needs a `section`" in result.error

    def test_ambiguous_section_is_refused(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        body = "## Log\n\na\n\n## Log\n\nb\n"
        server._store(NOTE, _note_template(NOTE, "Dupes", body, NB, now_iso()))
        result = asyncio.run(client.append_to_note(NOTE, "x", section="Log"))
        assert isinstance(result, JoplinError)
        assert "ambiguous" in result.error
        assert "get_note_outline" in (result.hint or "")

    def test_missing_section_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.append_to_note(NOTE, "x", section="Nope"))
        assert isinstance(result, JoplinError)
        assert "not found" in result.error

    def test_empty_text_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.append_to_note(NOTE, ""))
        assert isinstance(result, JoplinError)
        assert "empty" in result.error

    def test_bad_position_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.append_to_note(NOTE, "x", position="middle"))
        assert isinstance(result, JoplinError)
        assert "position must be" in result.error


# ---------------------------------------------------------------------------
# replace_in_note
# ---------------------------------------------------------------------------


class TestReplaceInNote:
    def test_replaces_a_unique_anchor(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.replace_in_note(NOTE, "Deployed.", "Rolled back."))
        assert result.changed is True
        assert "Rolled back." in server.body(NOTE)
        assert "Deployed." not in server.body(NOTE)

    def test_missing_anchor_is_refused_with_a_hint(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.replace_in_note(NOTE, "nowhere", "x"))
        assert isinstance(result, JoplinError)
        assert "not found" in result.error
        assert "get_note(raw=True)" in (result.hint or "")

    def test_whitespace_mismatch_is_explained(self, client: JoplinClient) -> None:
        result = asyncio.run(client.replace_in_note(NOTE, "  Deployed.  ", "x"))
        assert isinstance(result, JoplinError)
        assert "whitespace-trimmed" in result.error

    def test_multi_match_is_refused_and_lines_reported(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Dup", "x\n\ny\n\nx\n\nz\n\nx", NB, now_iso()),
        )
        result = asyncio.run(client.replace_in_note(NOTE, "x", "q"))
        assert isinstance(result, JoplinError)
        assert "matches 3 times" in result.error
        assert "lines 1, 5, 9" in result.error

    def test_replace_all_opts_into_every_occurrence(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Dup", "x\n\ny\n\nx", NB, now_iso()),
        )
        result = asyncio.run(client.replace_in_note(NOTE, "x", "q", replace_all=True))
        assert result.changed is True
        assert server.body(NOTE).count("q") == 2

    def test_empty_new_text_deletes_the_anchor(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.replace_in_note(NOTE, "Deployed.", ""))
        assert "Deleted" in result.detail
        assert "Deployed." not in server.body(NOTE)

    def test_empty_anchor_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.replace_in_note(NOTE, "", "x"))
        assert isinstance(result, JoplinError)
        assert "must not be empty" in result.error

    def test_dry_run_leaves_the_note_alone(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.items[NOTE]
        result = asyncio.run(
            client.replace_in_note(NOTE, "Deployed.", "x", dry_run=True)
        )
        assert result.dry_run is True
        assert result.changed is True
        assert server.items[NOTE] == before

    def test_resource_links_survive_an_unrelated_edit(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        body = f"see (:/{RESOURCE})\n\nDeployed."
        server._store(NOTE, _note_template(NOTE, "Res", body, NB, now_iso()))
        asyncio.run(client.replace_in_note(NOTE, "Deployed.", "Rolled back."))
        # The link is preserved byte for byte -- the rendered label never leaks
        # back into the stored body.
        assert f"(:/{RESOURCE})" in server.body(NOTE)


# ---------------------------------------------------------------------------
# replace_section
# ---------------------------------------------------------------------------


class TestReplaceSection:
    def test_replaces_section_content_keeping_the_heading(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.replace_section(NOTE, "Details", "- rewritten"))
        assert result.changed is True
        body = server.body(NOTE)
        assert "### Details" in body
        assert "- rewritten" in body
        assert "- one" not in body

    def test_touches_only_that_section(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.replace_section(NOTE, "Details", "- rewritten"))
        body = server.body(NOTE)
        # Everything outside the section is byte-identical.
        assert "Shipped the release." in body
        assert "## 2026-08-26" in body
        assert "Deployed." in body

    def test_preserves_resource_links_elsewhere(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        body = f"## Images\n\n### One\n\n(:/{RESOURCE})\n\n### Two\n\ntext"
        server._store(NOTE, _note_template(NOTE, "Res", body, NB, now_iso()))
        asyncio.run(client.replace_section(NOTE, "Two", "replaced"))
        assert f"(:/{RESOURCE})" in server.body(NOTE)

    def test_empty_text_clears_the_section(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.replace_section(NOTE, "Details", ""))
        body = server.body(NOTE)
        assert "- one" not in body
        assert "### Details" in body

    def test_dry_run_leaves_the_note_alone(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.items[NOTE]
        result = asyncio.run(
            client.replace_section(NOTE, "Details", "- new", dry_run=True)
        )
        assert result.dry_run is True
        assert server.items[NOTE] == before

    def test_ambiguous_section_is_refused(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Dup", "## Log\n\na\n\n## Log\n\nb", NB, now_iso()),
        )
        result = asyncio.run(client.replace_section(NOTE, "Log", "x"))
        assert isinstance(result, JoplinError)
        assert "ambiguous" in result.error


# ---------------------------------------------------------------------------
# Metadata preservation
# ---------------------------------------------------------------------------


class TestMetadataPreservation:
    def test_edit_keeps_all_metadata_fields(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(
                NOTE,
                "Keep",
                "line",
                NB,
                now_iso(),
                share_id="share-1",
                is_todo=True,
                due_ms=1790000000000,
            ),
        )
        before = server.metadata(NOTE)
        asyncio.run(client.append_to_note(NOTE, "added"))
        after = server.metadata(NOTE)
        for key in ("id", "parent_id", "is_todo", "todo_due", "share_id", "type_"):
            assert after[key] == before[key], key

    def test_edit_preserves_every_body_line(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.body(NOTE)
        asyncio.run(
            client.append_to_note(NOTE, "extra", section="Details", separator="\n")
        )
        after = server.body(NOTE)
        for line in before.split("\n"):
            assert line in after

    def test_timestamps_advance_on_write(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.append_to_note(NOTE, "extra"))
        updated = server.metadata(NOTE)["updated_time"]
        assert updated == server.metadata(NOTE)["user_updated_time"]


# ---------------------------------------------------------------------------
# To-do tools
# ---------------------------------------------------------------------------


class TestTodoTools:
    def test_set_due_date(self, client: JoplinClient, server: FakeJoplin) -> None:
        result = asyncio.run(client.set_todo(NOTE, due=DUE))
        assert result.is_todo is True
        assert result.todo_due == DUE
        assert server.metadata(NOTE)["is_todo"] == "1"

    def test_complete_then_reopen(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.set_todo(NOTE, completed=True))
        assert server.metadata(NOTE)["todo_completed"] != "0"
        asyncio.run(client.set_todo(NOTE, completed=False))
        assert server.metadata(NOTE)["todo_completed"] == "0"

    def test_clearing_todo_resets_everything(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.set_todo(NOTE, completed=True, due=DUE))
        asyncio.run(client.set_todo(NOTE, is_todo=False))
        meta = server.metadata(NOTE)
        assert meta["is_todo"] == "0"
        assert meta["todo_due"] == "0"
        assert meta["todo_completed"] == "0"

    def test_invalid_due_date_changes_nothing(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        before = server.items[NOTE]
        result = asyncio.run(client.set_todo(NOTE, due="soon"))
        assert isinstance(result, JoplinError)
        assert server.items[NOTE] == before

    def test_no_arguments_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.set_todo(NOTE))
        assert isinstance(result, JoplinError)
        assert "at least one" in result.error

    def test_create_note_as_todo_with_due(self, client: JoplinClient) -> None:
        result = asyncio.run(client.create_note("Task", "body", is_todo=True, due=DUE))
        assert result.is_todo is True
        assert result.todo_due == DUE

    def test_due_date_alone_implies_todo(self, client: JoplinClient) -> None:
        result = asyncio.run(client.create_note("Task", "body", due=DUE))
        assert result.is_todo is True

    def test_todo_filters_and_ordering(self, client: JoplinClient) -> None:
        plain = asyncio.run(client.create_note("Plain", "x"))
        todo = asyncio.run(client.create_note("Task", "x", is_todo=True))
        open_todos = asyncio.run(client.list_notes(todo="open"))
        assert [n.id for n in open_todos] == [todo.id]

        asyncio.run(client.set_todo(todo.id, completed=True))
        done = asyncio.run(client.list_notes(todo="done"))
        assert [n.id for n in done] == [todo.id]
        assert plain.id not in [n.id for n in done]

        page = asyncio.run(client.get_all_notes(todo="done", order_by="todo_completed"))
        assert [n.id for n in page.notes] == [todo.id]

    def test_invalid_todo_filter_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.list_notes(todo="maybe"))
        assert isinstance(result, JoplinError)
        assert "'all', 'open', or 'done'" in result.error

    def test_todo_marker_surfaces_in_summaries(self, client: JoplinClient) -> None:
        asyncio.run(client.set_todo(NOTE, due=DUE))
        summary = next(n for n in asyncio.run(client.list_notes()) if n.id == NOTE)
        assert summary.todo_marker == f"[todo] (due {DUE})"
        assert summary.todo_due == DUE

    def test_past_due_date_is_marked_overdue(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.set_todo(NOTE, due="2020-01-01"))
        summary = next(n for n in asyncio.run(client.list_notes()) if n.id == NOTE)
        assert summary.todo_marker == "[todo] (due 2020-01-01) OVERDUE"


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------


class TestNotebookTools:
    def test_get_notebook_lists_notes_and_sub_notebooks(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.create_notebook("Sub", parent_id=NB))
        result = asyncio.run(client.get_notebook(NB))
        assert [nb.title for nb in result.sub_notebooks] == ["Sub"]
        assert [n.title for n in result.notes] == ["Work Log"]

    def test_get_or_create_creates_missing_levels_only(
        self, client: JoplinClient
    ) -> None:
        first = asyncio.run(client.get_or_create_notebook("A/B/C"))
        assert first.created == ["A", "B", "C"]
        second = asyncio.run(client.get_or_create_notebook("A/B/C"))
        assert second.created == []
        assert second.notebook_id == first.notebook_id

    def test_get_or_create_rejects_empty_path(self, client: JoplinClient) -> None:
        assert isinstance(
            asyncio.run(client.get_or_create_notebook("  // ")), JoplinError
        )

    def test_update_notebook_refuses_circular_move(self, client: JoplinClient) -> None:
        parent = asyncio.run(client.get_or_create_notebook("P"))
        child = asyncio.run(client.get_or_create_notebook("P/Kid"))
        result = asyncio.run(
            client.update_notebook(parent.notebook_id, parent_id=child.notebook_id)
        )
        assert isinstance(result, JoplinError)
        assert "inside the notebook being moved" in result.error

    def test_update_notebook_refuses_self_move(self, client: JoplinClient) -> None:
        result = asyncio.run(client.update_notebook(NB, parent_id=NB))
        assert isinstance(result, JoplinError)
        assert "into itself" in result.error

    def test_rename_notebook(self, client: JoplinClient, server: FakeJoplin) -> None:
        asyncio.run(client.update_notebook(NB, title="Renamed"))
        assert server.items[NB].startswith("Renamed")

    def test_delete_non_empty_notebook_refuses_without_force(
        self, client: JoplinClient
    ) -> None:
        result = asyncio.run(client.delete_notebook(NB))
        assert isinstance(result, JoplinError)
        assert "force=True" in result.error

    def test_force_delete_removes_descendants_deepest_first(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.get_or_create_notebook("Work/Deep"))
        result = asyncio.run(client.delete_notebook(NB, force=True))
        assert NB not in server.items
        assert NOTE not in server.items
        assert "Deep" in result.deleted_children
        # The unrelated notebook survived.
        assert NB2 in server.items

    def test_delete_notebook_needs_a_valid_id(self, client: JoplinClient) -> None:
        assert isinstance(asyncio.run(client.delete_notebook("x")), JoplinError)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TestTagTools:
    def test_tag_lifecycle(self, client: JoplinClient, server: FakeJoplin) -> None:
        assert asyncio.run(client.get_note_tags(NOTE))[0].title == "ops"
        asyncio.run(client.remove_tag_from_note(TAG, NOTE))
        assert asyncio.run(client.get_note_tags(NOTE)) == []
        asyncio.run(client.add_tag_to_note(TAG, NOTE))
        assert asyncio.run(client.get_note_tags(NOTE))[0].title == "ops"

    def test_duplicate_tag_is_refused(self, client: JoplinClient) -> None:
        result = asyncio.run(client.add_tag_to_note(TAG, NOTE))
        assert isinstance(result, JoplinError)
        assert "already on note" in result.error

    def test_delete_tag_removes_its_links(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        result = asyncio.run(client.delete_tag(TAG))
        assert result.removed_from_notes == 1
        assert "f" * 32 not in server.items
        assert asyncio.run(client.get_note_tags(NOTE)) == []

    def test_list_notes_filters_by_tag(self, client: JoplinClient) -> None:
        assert len(asyncio.run(client.list_notes(tag="ops"))) == 1
        assert asyncio.run(client.list_notes(tag="absent")) == []

    def test_tag_summary_lists_tags(self, client: JoplinClient) -> None:
        notes = asyncio.run(client.list_notes(tag="ops"))
        assert notes[0].tags == ["ops"]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class TestResourceTools:
    def test_note_resources_are_listed(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Res", f"(:/{RESOURCE})", NB, now_iso()),
        )
        result = asyncio.run(client.get_note_resources(NOTE))
        assert [r.id for r in result.resources] == [RESOURCE]
        assert result.resources[0].mime == "image/png"

    def test_get_note_full_inlines_resources(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Res", f"(:/{RESOURCE})", NB, now_iso()),
        )
        result = asyncio.run(client.get_note_full(NOTE))
        assert len(result.resources) == 1
        assert result.resources[0].base64
        assert json.loads(json.dumps(result.resources[0].model_dump()))["size"] == 15

    def test_export_note_localises_filenames(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server._store(
            NOTE,
            _note_template(NOTE, "Res", f"img (:/{RESOURCE})", NB, now_iso()),
        )
        result = asyncio.run(client.export_note(NOTE))
        assert "photo.png" in result.body
        assert f":/{RESOURCE}" not in result.body
        assert [r.filename for r in result.resources] == ["photo.png"]

    def test_download_resource(self, client: JoplinClient) -> None:
        result = asyncio.run(client.download_resource(RESOURCE))
        assert result.mime == "image/png"
        assert result.base64

    def test_download_missing_resource_is_reported(self, client: JoplinClient) -> None:
        result = asyncio.run(client.download_resource("9" * 32))
        assert isinstance(result, JoplinError)
        assert "not found" in result.error

    def test_oversized_resource_is_refused(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        server.resource_items[RESOURCE] = RESOURCE_ITEM.replace(
            "size: 12", "size: 999999999"
        )
        asyncio.run(client._ensure_resource_index(force=True))
        result = asyncio.run(client.download_resource(RESOURCE))
        assert isinstance(result, JoplinError)
        assert "too large" in result.error

    def test_resource_id_is_validated(self, client: JoplinClient) -> None:
        assert isinstance(asyncio.run(client.get_resource_info("nope")), JoplinError)


# ---------------------------------------------------------------------------
# Index behaviour
# ---------------------------------------------------------------------------


class TestIndex:
    def test_second_read_reuses_the_cached_index(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.list_notes())
        server.puts.clear()
        asyncio.run(client.list_notes())
        # Nothing re-fetched: the TTL was not up.
        assert NOTE not in server.puts

    def test_force_refresh_picks_up_a_new_note(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        assert len(asyncio.run(client.list_notes())) == 1
        server._store("9" * 32, _note_template("9" * 32, "New", "x", NB, now_iso()))
        # ``force_refresh`` awaits the sync instead of serving the stale index,
        # which is what the create and delete paths rely on.
        asyncio.run(client.index(force_refresh=True))
        assert len(asyncio.run(client.get_all_notes()).notes) == 2

    def test_expired_ttl_serves_stale_and_refreshes_in_background(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        assert len(asyncio.run(client.list_notes())) == 1
        server._store("9" * 32, _note_template("9" * 32, "New", "x", NB, now_iso()))
        client.index_ttl = 0.0

        async def scenario() -> tuple[int, int]:
            stale = len(await client.list_notes())
            if client._refresh_task is not None:
                await client._refresh_task
            return stale, len(await client.list_notes())

        stale_count, fresh_count = asyncio.run(scenario())
        # Reads never block on a sync, so the first one is still the old index.
        assert stale_count == 1
        assert fresh_count == 2

    def test_deleted_note_leaves_the_index(
        self, client: JoplinClient, server: FakeJoplin
    ) -> None:
        asyncio.run(client.delete_note(NOTE))
        client.index_ttl = 0.0
        assert asyncio.run(client.list_notes()) == []


# ---------------------------------------------------------------------------
# Root-notebook scoping
# ---------------------------------------------------------------------------


class TestRootScoping:
    def test_notes_outside_the_root_are_hidden(self, server: FakeJoplin) -> None:
        scoped = _make_client(server, root_notebook_id=NB)
        assert asyncio.run(scoped.list_notes())
        assert [nb.id for nb in asyncio.run(scoped.list_notebooks())] == [NB]

    def test_writes_outside_the_root_are_refused(self, server: FakeJoplin) -> None:
        scoped = _make_client(server, root_notebook_id=NB2)
        result = asyncio.run(scoped.delete_note(NOTE))
        assert isinstance(result, JoplinError)
        assert "outside the configured root" in result.error

    def test_configured_root_cannot_be_deleted(self, server: FakeJoplin) -> None:
        scoped = _make_client(server, root_notebook_id=NB)
        result = asyncio.run(scoped.delete_notebook(NB, force=True))
        assert isinstance(result, JoplinError)
        assert "root notebook" in result.error
