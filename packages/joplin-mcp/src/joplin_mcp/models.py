"""Structured payloads returned by the Joplin MCP tools.

Keeping results as models (rather than pre-rendered text) means a client can
read one field without parsing a blob, which matters for the edit tools: a
report carries the char/line/heading deltas *and* the numbered lines around the
change, so a write can be verified without re-reading the note.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JoplinError(BaseModel):
    """Structured error response."""

    error: str
    hint: str | None = None


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------


class NotebookSummary(BaseModel):
    """A notebook (folder) on the Joplin server."""

    id: str
    title: str
    parent_id: str = ""


class NotebookDetail(BaseModel):
    """A notebook with its notes and immediate sub-notebooks."""

    id: str
    title: str
    parent_id: str = ""
    parent_title: str = ""
    updated_time: str = ""
    note_count: int = 0
    sub_notebooks: list[NotebookSummary] = Field(default_factory=list)
    notes: list[NoteSummary] = Field(default_factory=list)


class NotebookCreatedResponse(BaseModel):
    """Response after creating a notebook."""

    id: str
    message: str


class NotebookPathResult(BaseModel):
    """Response after resolving a notebook path."""

    path: str
    notebook_id: str
    title: str
    created: list[str] = Field(default_factory=list)
    message: str


class NotebookUpdatedResponse(BaseModel):
    """Response after updating a notebook."""

    id: str
    message: str


class NotebookDeletedResponse(BaseModel):
    """Response after deleting a notebook."""

    id: str
    message: str
    deleted_children: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class NoteSummary(BaseModel):
    """Compact note summary for list/search results."""

    id: str
    title: str
    notebook_id: str = ""
    notebook_title: str = ""
    is_todo: bool = False
    todo_marker: str = ""
    todo_due: str = ""
    updated_time: str = ""
    tags: list[str] = Field(default_factory=list)
    preview: str = Field(
        default="",
        description="A short extract: load whole note with `get_note` before editing",
    )


class ResourceRef(BaseModel):
    """A resource referenced from a note body."""

    id: str
    title: str = ""
    mime: str = ""
    size: int = 0
    resolved: bool = True


class NoteDetail(BaseModel):
    """Full note detail returned by get_note."""

    id: str
    title: str
    body: str = ""
    notebook_id: str = ""
    notebook_title: str = ""
    is_todo: bool = False
    todo_marker: str = ""
    todo_due: str = ""
    todo_completed: str = ""
    created_time: str = ""
    updated_time: str = ""
    raw: bool = Field(
        default=False,
        description="True when `body` is verbatim (no resource labels substituted)",
    )
    resource_refs: list[ResourceRef] = Field(default_factory=list)


class NotePage(BaseModel):
    """One page of notes, with the pagination state needed to fetch more."""

    notes: list[NoteSummary] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    order_by: str = "updated_time"
    order_dir: str = "desc"
    notebook_id: str = ""
    notebook_title: str = ""


class NoteBatch(BaseModel):
    """Result of reading several notes at once."""

    notes: list[NoteDetail] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class NoteCreatedResponse(BaseModel):
    """Response after creating a note."""

    id: str
    title: str
    is_todo: bool = False
    todo_due: str = ""
    message: str


class NoteUpdatedResponse(BaseModel):
    """Response after replacing a note's title, body, or notebook."""

    id: str
    title: str
    message: str


class NoteDeletedResponse(BaseModel):
    """Response after deleting a note."""

    id: str
    title: str
    message: str


class TodoUpdateResponse(BaseModel):
    """Response after changing a note's to-do state."""

    id: str
    title: str
    is_todo: bool = False
    todo_due: str = ""
    todo_completed: str = ""
    changes: list[str] = Field(default_factory=list)
    message: str


# ---------------------------------------------------------------------------
# Partial edits
# ---------------------------------------------------------------------------


class EditReport(BaseModel):
    """What a body edit did: deltas, the line it landed on, and its context.

    Returned by ``append_to_note``, ``replace_in_note`` and ``replace_section``.
    With ``dry_run`` the numbers describe what *would* have happened and nothing
    was written.
    """

    id: str
    title: str
    action: str
    dry_run: bool = False
    changed: bool = False
    skipped: bool = False
    detail: str = ""
    chars_before: int = 0
    chars_after: int = 0
    lines_before: int = 0
    lines_after: int = 0
    headings_before: int = 0
    headings_after: int = 0
    line: int = 0
    context_lines: list[str] = Field(default_factory=list)
    message: str = ""


class OutlineEntry(BaseModel):
    """One heading in a note outline."""

    level: int
    title: str
    line: int
    chars: int


class NoteOutline(BaseModel):
    """A note's heading map: line numbers and span sizes, no body text."""

    id: str
    title: str
    notebook_id: str = ""
    notebook_title: str = ""
    updated_time: str = ""
    body_chars: int = 0
    body_lines: int = 0
    headings: list[OutlineEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagSummary(BaseModel):
    """A tag on the Joplin server."""

    id: str
    title: str


class NoteTagLink(BaseModel):
    """A link between a note and a tag."""

    id: str
    note_id: str
    tag_id: str


class TagCreatedResponse(BaseModel):
    """Response after creating a tag."""

    id: str
    message: str


class TagDeletedResponse(BaseModel):
    """Response after deleting a tag."""

    id: str
    message: str
    removed_from_notes: int = 0


class TagAddedResponse(BaseModel):
    """Response after adding a tag to a note."""

    id: str
    message: str


class TagRemovedResponse(BaseModel):
    """Response after removing a tag from a note."""

    id: str
    message: str


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


class NoteResources(BaseModel):
    """Resources referenced by a note."""

    id: str
    title: str
    resources: list[ResourceRef] = Field(default_factory=list)


class ResourceInfo(BaseModel):
    """Metadata for an attached resource."""

    id: str
    title: str = ""
    mime: str = ""
    size: int = 0
    file_extension: str = ""
    created_time: str = ""
    updated_time: str = ""


class ResourceContent(ResourceInfo):
    """A resource plus its bytes, base64-encoded."""

    base64: str = ""
    error: str | None = None


class NoteWithResources(BaseModel):
    """A note with every referenced resource embedded."""

    note: NoteDetail
    resources: list[ResourceContent] = Field(default_factory=list)


class ExportedResource(BaseModel):
    """A resource as emitted by ``export_note``."""

    filename: str
    mime: str = ""
    size: int = 0
    base64: str = ""


class NoteExport(BaseModel):
    """A note rendered for writing to disk: local filenames, base64 blobs."""

    title: str
    id: str
    body: str = ""
    resources: list[ExportedResource] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class PingResponse(BaseModel):
    """Result of a connectivity check."""

    ok: bool
    server_url: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
