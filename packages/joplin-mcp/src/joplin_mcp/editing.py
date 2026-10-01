"""Pure markdown helpers for server-side note edits.

Partial edits are resolved here rather than in the model: the caller sends only
the fragment it wants added or changed, never the surrounding body. That keeps
large notes cheap to edit and makes it impossible to corrupt untouched text --
including ``:/<resource-id>`` links, which the read tools render as
human-readable names.

Nothing in this module imports the rest of the package or touches the network,
so the whole module is covered by the dependency-free test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$")

_FENCES = ("```", "~~~")


class SectionError(ValueError):
    """A section reference could not be resolved to exactly one heading."""


@dataclass(frozen=True, slots=True)
class Heading:
    """One markdown heading found in a note body."""

    level: int
    title: str
    line: int  # 1-based line number of the heading itself
    start: int  # character offset of the heading line
    content_start: int  # offset just past the heading line
    chars: int = 0  # span owned by this heading; set by :func:`outline`


@dataclass(frozen=True, slots=True)
class Section:
    """A heading together with the span of the body it owns."""

    level: int
    title: str
    line: int
    start: int
    content_start: int
    end: int

    @property
    def label(self) -> str:
        """Human-readable reference used in edit reports and errors."""
        return f"'{'#' * self.level} {self.title}' (line {self.line})"


def headings(body: str) -> list[Heading]:
    """Markdown headings outside fenced code blocks, in document order.

    A heading needs at least one space after its hashes and some text after
    that; ``#hashtag`` lines and ``#`` alone are not headings.
    """
    result: list[Heading] = []
    fence = ""
    offset = 0
    for lineno, line in enumerate(body.split("\n"), start=1):
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
        elif stripped.startswith(_FENCES):
            fence = stripped[:3]
        else:
            match = _HEADING_RE.match(line)
            if match:
                result.append(
                    Heading(
                        level=len(match.group(1)),
                        title=match.group(2).strip(),
                        line=lineno,
                        start=offset,
                        content_start=min(offset + len(line) + 1, len(body)),
                    )
                )
        offset += len(line) + 1
    return result


def section_end(heads: list[Heading], index: int, body_len: int) -> int:
    """Offset where a section ends: at the next heading of the same or higher level.

    A section therefore includes its subsections.
    """
    level = heads[index].level
    for nxt in heads[index + 1 :]:
        if nxt.level <= level:
            return nxt.start
    return body_len


def outline(body: str) -> list[Heading]:
    """Headings annotated with the size of the span they own.

    This is what :func:`heading_count` and the outline report are built from, so
    a caller can pick a section by line number without reading the body.
    """
    heads = headings(body)
    return [
        replace(head, chars=section_end(heads, i, len(body)) - head.start)
        for i, head in enumerate(heads)
    ]


def heading_count(body: str) -> int:
    """Number of markdown headings in *body*."""
    return len(headings(body))


def find_section(body: str, section: str) -> Section:
    """Resolve a section reference to a span, or raise :class:`SectionError`.

    The reference may be bare heading text (``"Hosts"``) or the full line
    (``"## Hosts"``); when the hashes are given the level must match too.
    Matching is exact, then case-insensitive, then a unique case-insensitive
    substring. Anything missing or ambiguous is refused rather than guessed.
    """
    heads = headings(body)
    if not heads:
        msg = "Note has no markdown headings; omit `section` to edit the whole body."
        raise SectionError(msg)

    wanted = section.strip()
    level = 0
    match = re.match(r"^(#{1,6})[ \t]*", wanted)
    if match:
        level = len(match.group(1))
        wanted = wanted[match.end() :].strip()
    if not wanted:
        msg = (
            f"Invalid section reference: '{section}' "
            "(expected e.g. 'Hosts' or '## Hosts')."
        )
        raise SectionError(msg)

    def candidates(predicate) -> list[int]:
        return [
            i
            for i, head in enumerate(heads)
            if (not level or head.level == level) and predicate(head.title)
        ]

    lowered = wanted.lower()
    found = (
        candidates(lambda t: t == wanted)
        or candidates(lambda t: t.lower() == lowered)
        or candidates(lambda t: lowered in t.lower())
    )

    if not found:
        listed = ", ".join(f"'{h.title}'" for h in heads[:30])
        more = (
            f" (+{len(heads) - 30} more, see get_note_outline)"
            if len(heads) > 30
            else ""
        )
        msg = f"Section '{section}' not found. Headings: {listed}{more}."
        raise SectionError(msg)
    if len(found) > 1:
        where = "; ".join(
            f"line {heads[i].line}: {'#' * heads[i].level} {heads[i].title}"
            for i in found[:10]
        )
        msg = (
            f"Section '{section}' is ambiguous ({len(found)} matches): {where}. "
            "Give the full heading text with its '#' prefix, "
            "or use replace_in_note."
        )
        raise SectionError(msg)

    head = heads[found[0]]
    return Section(
        level=head.level,
        title=head.title,
        line=head.line,
        start=head.start,
        content_start=head.content_start,
        end=section_end(heads, found[0], len(body)),
    )


def splice(
    body: str, start: int, end: int, text: str, position: str, separator: str
) -> tuple[str, int]:
    """Insert *text* at the start or end of the ``body[start:end]`` span.

    Returns the new body and the offset the inserted text landed at. Blank-line
    padding is normalised inside the touched span only, so nothing outside it is
    rewritten.
    """
    before, segment, after = body[:start], body[start:end], body[end:]
    payload, inner = text.strip("\n"), segment.strip("\n")
    lead = "\n" if before and not before.endswith("\n\n") else ""
    if position == "start":
        merged = f"{payload}{separator}{inner}" if inner else payload
        at = len(before) + len(lead)
    else:
        merged = f"{inner}{separator}{payload}" if inner else payload
        at = len(before) + len(lead) + len(merged) - len(payload)
    tail = "\n\n" if after else ""
    return f"{before}{lead}{merged}{tail}{after}", at


def insert_block(body: str, cut: int, text: str) -> tuple[str, int]:
    """Insert *text* as a standalone block at offset *cut*.

    Strictly additive: nothing already in the body is rewritten, and blank lines
    are added only where the join would otherwise glue the block to its
    neighbour. This is what the ``before``/``after`` positions use, and it is
    why they are safer than ``start``/``end`` -- those normalise blank-line
    padding inside the span they edit.
    """
    before, after = body[:cut], body[cut:]
    payload = text.strip("\n")
    if not before or before.endswith("\n\n"):
        lead = ""
    elif before.endswith("\n"):
        lead = "\n"
    else:
        lead = "\n\n"
    tail = "\n\n" if after else ""
    return f"{before}{lead}{payload}{tail}{after}", len(before) + len(lead)


def line_of(body: str, offset: int) -> int:
    """1-based line number containing *offset*."""
    return body.count("\n", 0, max(offset, 0)) + 1


def line_count(body: str) -> int:
    """Number of lines in *body* (always at least 1)."""
    return body.count("\n") + 1


def context_lines(body: str, offset: int, radius: int = 4) -> list[str]:
    """Numbered lines around *offset*, so a write can be checked in place.

    Each entry looks like ``"12 | text"``; long lines are truncated so a report
    never turns into a wall of text.
    """
    lines = body.split("\n")
    target = line_of(body, offset) - 1
    lo, hi = max(0, target - radius), min(len(lines), target + radius + 1)
    width = len(str(hi))
    out = []
    for i in range(lo, hi):
        text = lines[i] if len(lines[i]) <= 200 else lines[i][:197] + "..."
        out.append(f"{str(i + 1).rjust(width)} | {text}")
    return out
