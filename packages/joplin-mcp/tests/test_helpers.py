"""Unit tests for the pure helpers behind the partial-edit tools.

These cover markdown section resolution, splicing, edit reports and to-do state.
They import no I/O and need no server, so ``python tests/test_helpers.py`` (or
pytest) runs anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from joplin_mcp.client import (  # noqa: E402
    _find_resource_refs,
    _parse_joplin_item,
    _parse_resource_metadata,
    _serialize_item,
)
from joplin_mcp.editing import (  # noqa: E402
    SectionError,
    context_lines,
    find_section,
    heading_count,
    headings,
    insert_block,
    line_count,
    line_of,
    outline,
    splice,
)
from joplin_mcp.todos import (  # noqa: E402
    completed_value,
    due_value,
    filter_by_todo,
    ms_to_date,
    parse_due,
    todo_marker,
)

NOTE_ID = "0123456789abcdef0123456789abcdef"

LOG = """# Work Log

Notes as they happen.

## 2026-08-25

Shipped the
release.

### Details

- one
- two

## 2026-08-26

Deployed.
"""


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


class TestHeadings:
    def test_finds_headings_with_lines(self) -> None:
        found = headings(LOG)
        assert [h.title for h in found] == [
            "Work Log",
            "2026-08-25",
            "Details",
            "2026-08-26",
        ]
        assert [h.level for h in found] == [1, 2, 3, 2]
        assert [h.line for h in found] == [1, 5, 10, 15]

    def test_ignores_headings_in_code_fences(self) -> None:
        body = (
            "# Real\n\n```\n# Not a heading\n```\n\n~~~\n## Also not\n~~~\n\n## Yes\n"
        )
        assert [h.title for h in headings(body)] == ["Real", "Yes"]

    def test_requires_space_after_hashes(self) -> None:
        assert heading_count("#hashtag\n#\n") == 0
        assert heading_count("###### deep\n") == 1

    def test_outline_measures_span_with_subsections(self) -> None:
        spans = {h.title: h.chars for h in outline(LOG)}
        # The H2 owns its own H3 subsection.
        assert spans["2026-08-25"] > spans["Details"]
        assert spans["Details"] < spans["2026-08-25"]

    def test_heading_count_ignores_fences(self) -> None:
        assert heading_count("```\n# x\n```\n") == 0


# ---------------------------------------------------------------------------
# Section resolution
# ---------------------------------------------------------------------------


class TestFindSection:
    def test_bare_title(self) -> None:
        section = find_section(LOG, "Details")
        assert section.level == 3
        assert section.label == "'### Details' (line 10)"

    def test_full_line_with_level(self) -> None:
        assert find_section(LOG, "### Details").title == "Details"

    def test_level_mismatch_is_refused(self) -> None:
        with pytest.raises(SectionError, match="not found"):
            find_section(LOG, "### 2026-08-25")

    def test_case_insensitive_fallback(self) -> None:
        assert find_section(LOG, "details").title == "Details"

    def test_unique_substring_fallback(self) -> None:
        assert find_section(LOG, "Work").title == "Work Log"

    def test_missing_section_lists_headings(self) -> None:
        with pytest.raises(SectionError) as exc:
            find_section(LOG, "Nope")
        assert "not found" in str(exc.value)
        assert "Work Log" in str(exc.value)

    def test_ambiguous_section_is_refused_not_guessed(self) -> None:
        body = "## Log\n\na\n\n## Entries\n\nb\n\n## Log\n\nc\n"
        with pytest.raises(SectionError) as exc:
            find_section(body, "Log")
        message = str(exc.value)
        assert "ambiguous" in message
        assert "2 matches" in message
        # The message must locate both candidates so a caller can disambiguate.
        assert "line 1" in message and "line 9" in message

    def test_exact_match_wins_over_a_longer_heading(self) -> None:
        # "Log" appears as a prefix of "Log Extended"; the exact heading wins
        # instead of matching both.
        body = "## Log\n\na\n\n## Log Extended\n\nb\n"
        assert find_section(body, "Log").line == 1

    def test_section_end_stops_at_next_same_level(self) -> None:
        section = find_section(LOG, "2026-08-25")
        body_end = section.end
        assert LOG[section.start : body_end].startswith("## 2026-08-25")
        # The next H2 is not part of this section.
        assert "2026-08-26" not in LOG[section.start : body_end]

    def test_section_end_includes_subsections(self) -> None:
        section = find_section(LOG, "2026-08-25")
        assert "Details" in LOG[section.start : section.end]

    def test_last_section_runs_to_end(self) -> None:
        section = find_section(LOG, "2026-08-26")
        assert section.end == len(LOG)

    def test_body_without_headings(self) -> None:
        with pytest.raises(SectionError, match="no markdown headings"):
            find_section("just text", "Hosts")

    def test_hash_only_reference_is_refused(self) -> None:
        with pytest.raises(SectionError, match="Invalid section reference"):
            find_section(LOG, "###")


# ---------------------------------------------------------------------------
# Splicing
# ---------------------------------------------------------------------------


class TestSplice:
    def test_append_at_end_of_section(self) -> None:
        section = find_section(LOG, "Details")
        body, _ = splice(
            LOG, section.content_start, section.end, "- three", "end", "\n"
        )
        assert body.count("- three") == 1
        assert body.index("- three") < body.index("## 2026-08-26")

    def test_append_at_start_of_note(self) -> None:
        body, offset = splice(LOG, 0, len(LOG), "# New\n\ntext", "start", "\n\n")
        assert body.startswith("# New")
        assert offset == 0
        # The rest of the note survives intact.
        assert "## 2026-08-26" in body
        assert "Shipped the" in body

    def test_blank_line_padding_only_inside_touched_span(self) -> None:
        body, _ = splice(LOG, 0, len(LOG), "top", "start", "\n\n")
        # The untouched original spacing below is unchanged.
        assert "\n\nNotes as they happen.\n\n## 2026-08-25" in body

    def test_separator_is_honoured(self) -> None:
        section = find_section(LOG, "Details")
        body, _ = splice(
            LOG, section.content_start, section.end, "| row |", "end", "\n"
        )
        assert "- two\n| row |" in body

    def test_appending_to_empty_span(self) -> None:
        body, offset = splice(LOG, 0, 0, "only", "end", "\n\n")
        assert body.startswith("only")
        assert offset == 0


class TestInsertBlock:
    def test_insert_before_section_is_strictly_additive(self) -> None:
        target = find_section(LOG, "2026-08-25")
        body, offset = insert_block(LOG, target.start, "## 2026-08-27\n\nDeployed.")
        assert body.index("## 2026-08-27") < body.index("## 2026-08-25")
        assert offset == body.index("## 2026-08-27")
        # Nothing that was already there moved relative to its neighbours.
        assert "Notes as they happen.\n\n## 2026-08-27" in body

    def test_insert_before_first_section_beats_preamble(self) -> None:
        # A newest-first log whose first heading is preceded by a preamble:
        # "before" puts the new entry at the top, not above the preamble.
        target = find_section(LOG, "Work Log")
        body, _ = insert_block(LOG, target.start, "## 2026-08-27\n\nx")
        assert body.index("## 2026-08-27") < body.index("# Work Log")
        assert body.index("# Work Log") < body.index("Notes as they happen.")

    def test_insert_after_section_past_subsections(self) -> None:
        target = find_section(LOG, "2026-08-25")
        body, _ = insert_block(LOG, target.end, "## Next\n\nx")
        assert body.index("## Next") > body.index("- two")
        assert body.index("## Next") < body.index("## 2026-08-26")

    def test_blank_lines_added_only_where_needed(self) -> None:
        # One newline already separates the block from its neighbour, so only
        # the blank line the join needs is added.
        body, _ = insert_block("head\n", 5, "tail")
        assert body == "head\n\ntail"

    def test_blank_line_added_when_neighbour_is_glued(self) -> None:
        body, _ = insert_block("head", 4, "tail")
        assert body == "head\n\ntail"

    def test_no_extra_blank_line_when_already_padded(self) -> None:
        body, _ = insert_block("head\n\n", 6, "tail")
        assert body == "head\n\ntail"


# ---------------------------------------------------------------------------
# Edit reports
# ---------------------------------------------------------------------------


class TestContextLines:
    def test_numbers_lines_around_offset(self) -> None:
        lines = context_lines(LOG, 0, radius=1)
        assert lines[0].endswith("# Work Log")
        assert all(" | " in line for line in lines)

    def test_reports_line_numbers(self) -> None:
        assert line_of(LOG, 0) == 1
        assert line_of(LOG, LOG.index("## 2026-08-26")) == 15

    def test_line_count(self) -> None:
        assert line_count("a\nb\nc") == 3
        assert line_count("") == 1

    def test_long_lines_truncated(self) -> None:
        body = "x" * 500
        assert len(context_lines(body, 0)[0]) < 220


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------


def _note_raw(body: str = "hello\n\nworld") -> str:
    return (
        f"Test Note\n\n{body}\n\n"
        f"id: {NOTE_ID}\n"
        f"parent_id: {'b' * 32}\n"
        f"created_time: 2026-01-01T00:00:00.000Z\n"
        f"updated_time: 2026-01-02T00:00:00.000Z\n"
        f"is_todo: 1\n"
        f"todo_due: 1790000000000\n"
        f"todo_completed: 0\n"
        f"share_id:\n"
        f"deleted_time: 0\n"
        f"type_: 1"
    )


class TestItemParsing:
    def test_parses_note(self) -> None:
        parsed = _parse_joplin_item(_note_raw())
        assert parsed["id"] == NOTE_ID
        assert parsed["title"] == "Test Note"
        assert parsed["body"] == "hello\n\nworld"
        assert parsed["type"] == 1
        assert parsed["is_todo"] is True
        assert parsed["todo_due"] == 1790000000000

    def test_body_containing_an_id_line_is_not_truncated(self) -> None:
        # The metadata block is the trailing one; a body line that looks like
        # `id: <32 hex>` must not swallow the rest of the note.
        body = f"before\n\nid: {'c' * 32}\n\nafter"
        parsed = _parse_joplin_item(_note_raw(body))
        assert parsed["body"] == body
        assert parsed["id"] == NOTE_ID

    def test_body_ending_in_metadata_shaped_lines_is_not_truncated(self) -> None:
        body = "Summary:\nstatus: final\nowner: me"
        parsed = _parse_joplin_item(_note_raw(body))
        assert parsed["body"] == body

    def test_empty_body(self) -> None:
        parsed = _parse_joplin_item(_note_raw(""))
        assert parsed["body"] == ""

    def test_strict_mode_refuses_an_unparseable_item(self) -> None:
        with pytest.raises(ValueError, match="metadata block"):
            _parse_joplin_item("no metadata here at all", strict=True)

    def test_round_trip_preserves_metadata(self) -> None:
        raw = _note_raw("body text")
        parsed = _parse_joplin_item(raw)
        written = _serialize_item(parsed["title"], "changed", parsed["metadata"])
        again = _parse_joplin_item(written)
        assert again["body"] == "changed"
        assert again["id"] == NOTE_ID
        assert again["metadata"]["share_id"] == ""

    def test_parses_resource_metadata(self) -> None:
        raw = (
            "photo.jpg\n\n"
            "id: 0123456789abcdef0123456789abcdef\n"
            "mime: image/jpeg\n"
            "size: 2048\n"
            "file_extension: .jpg\n"
            "created_time: 2026-01-01T00:00:00.000Z\n"
            "updated_time: 2026-01-01T00:00:00.000Z\n"
            "type_: 9"
        )
        res = _parse_resource_metadata(raw)
        assert res["title"] == "photo.jpg"
        assert res["mime"] == "image/jpeg"
        assert res["size"] == 2048
        assert res["type"] == 9

    def test_resource_ref_dedupe(self) -> None:
        rid = "0123456789abcdef0123456789abcdef"
        body = f'a (:/{rid}) b (:/{rid}) c <img src=":/{rid}">'
        assert _find_resource_refs(body) == [rid]

    def test_no_resource_refs(self) -> None:
        assert _find_resource_refs("plain text") == []


# ---------------------------------------------------------------------------
# To-do state
# ---------------------------------------------------------------------------


class TestTodoHelpers:
    def test_parse_bare_date_is_utc_midnight(self) -> None:
        assert parse_due("2026-08-26") == 1787702400000

    def test_parse_iso_timestamp(self) -> None:
        assert parse_due("2026-08-26T12:00:00Z") == 1787745600000

    def test_empty_due_is_unset(self) -> None:
        assert parse_due("") == 0

    def test_junk_due_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid due date"):
            parse_due("next tuesday")

    def test_marker_for_plain_note_is_empty(self) -> None:
        assert todo_marker({"is_todo": False, "todo_due": 1}) == ""

    def test_marker_with_future_due_date(self) -> None:
        future = parse_due("2030-01-15")
        assert todo_marker({"is_todo": True, "todo_due": future}) == (
            "[todo] (due 2030-01-15)"
        )

    def test_marker_overdue(self) -> None:
        assert "OVERDUE" in todo_marker({"is_todo": True, "todo_due": 1000})

    def test_marker_done(self) -> None:
        marker = todo_marker({"is_todo": True, "todo_completed": 1790000000000})
        assert marker == "[done 2026-09-21]"

    def test_values_tolerate_missing_fields(self) -> None:
        assert due_value({"metadata": {}}) == 0
        assert completed_value({}) == 0
        assert due_value({"todo_due": "junk"}) == 0

    def test_filter_by_todo(self) -> None:
        notes = [
            {"id": "1", "is_todo": True, "todo_completed": 0},
            {"id": "2", "is_todo": True, "todo_completed": 5},
            {"id": "3", "is_todo": False},
        ]
        assert [n["id"] for n in filter_by_todo(notes, "open")[0]] == ["1"]
        assert [n["id"] for n in filter_by_todo(notes, "done")[0]] == ["2"]
        assert [n["id"] for n in filter_by_todo(notes, "all")[0]] == ["1", "2"]
        assert len(filter_by_todo(notes, None)[0]) == 3

    def test_filter_rejects_unknown_state(self) -> None:
        _, err = filter_by_todo([], "maybe")
        assert err is not None

    def test_ms_to_date(self) -> None:
        assert ms_to_date(1790000000000) == "2026-09-21"


def main() -> int:
    """Run the suite with plain pytest, so no server is needed."""
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
