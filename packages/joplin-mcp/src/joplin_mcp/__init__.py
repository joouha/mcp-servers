"""Joplin MCP Server.

An MCP server for managing notes, notebooks, tags, and attachments on a Joplin
Server.

The interesting tools are the partial editors -- ``append_to_note``,
``replace_in_note`` and ``replace_section``. They resolve the edit server-side
so the caller sends only the fragment it wants added or changed, which keeps
large notes cheap to edit and makes it impossible to corrupt untouched text
(including ``:/<resource-id>`` links, which the read tools render as names).
Every write reports what it did, and every write can be dry-run first.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Context, FastMCP

from .client import (
    MAX_BATCH_NOTES,
    MAX_RESOURCE_SIZE,
    TYPE_FOLDER,
    TYPE_NOTE,
    TYPE_NOTE_TAG,
    TYPE_TAG,
    JoplinClient,
)
from .editing import (
    Heading,
    Section,
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
from .models import (
    EditReport,
    ExportedResource,
    JoplinError,
    NotebookCreatedResponse,
    NotebookDeletedResponse,
    NotebookDetail,
    NotebookPathResult,
    NotebookSummary,
    NotebookUpdatedResponse,
    NoteBatch,
    NoteCreatedResponse,
    NoteDeletedResponse,
    NoteDetail,
    NoteExport,
    NoteOutline,
    NotePage,
    NoteResources,
    NoteSummary,
    NoteTagLink,
    NoteUpdatedResponse,
    NoteWithResources,
    OutlineEntry,
    PingResponse,
    ResourceContent,
    ResourceInfo,
    ResourceRef,
    TagAddedResponse,
    TagCreatedResponse,
    TagDeletedResponse,
    TagRemovedResponse,
    TagSummary,
    TodoUpdateResponse,
)
from .todos import (
    TODO_STATES,
    completed_value,
    due_value,
    filter_by_todo,
    ms_to_date,
    now_iso,
    now_ms,
    parse_due,
    todo_marker,
)

log = logging.getLogger(__name__)

__all__ = [
    "MAX_BATCH_NOTES",
    "MAX_RESOURCE_SIZE",
    "TODO_STATES",
    "TYPE_FOLDER",
    "TYPE_NOTE",
    "TYPE_NOTE_TAG",
    "TYPE_TAG",
    "EditReport",
    "ExportedResource",
    "Heading",
    "JoplinClient",
    "JoplinError",
    "NotebookCreatedResponse",
    "NotebookDeletedResponse",
    "NotebookDetail",
    "NotebookPathResult",
    "NotebookSummary",
    "NotebookUpdatedResponse",
    "NoteBatch",
    "NoteCreatedResponse",
    "NoteDeletedResponse",
    "NoteDetail",
    "NoteExport",
    "NoteOutline",
    "NotePage",
    "NoteResources",
    "NoteSummary",
    "NoteTagLink",
    "NoteUpdatedResponse",
    "NoteWithResources",
    "OutlineEntry",
    "PingResponse",
    "ResourceContent",
    "ResourceInfo",
    "ResourceRef",
    "Section",
    "SectionError",
    "TagAddedResponse",
    "TagCreatedResponse",
    "TagDeletedResponse",
    "TagRemovedResponse",
    "TagSummary",
    "TodoUpdateResponse",
    "completed_value",
    "context_lines",
    "due_value",
    "filter_by_todo",
    "find_section",
    "get_client",
    "heading_count",
    "headings",
    "insert_block",
    "line_count",
    "line_of",
    "main",
    "mcp",
    "ms_to_date",
    "now_iso",
    "now_ms",
    "outline",
    "parse_due",
    "splice",
    "todo_marker",
]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create a single JoplinClient for the server's lifetime."""
    url = os.environ.get("JOPLIN_SERVER_URL", "")
    email = os.environ.get("JOPLIN_EMAIL", "")
    password = os.environ.get("JOPLIN_PASSWORD", "")
    root_notebook_id = os.environ.get("JOPLIN_NOTEBOOK_ID", "")

    if not url:
        msg = "JOPLIN_SERVER_URL environment variable is required"
        raise RuntimeError(msg)
    if not email or not password:
        msg = "JOPLIN_EMAIL and JOPLIN_PASSWORD environment variables are required"
        raise RuntimeError(msg)

    client = JoplinClient(
        url=url,
        email=email,
        password=password,
        root_notebook_id=root_notebook_id,
    )
    try:
        # Start the sync in the background so the first tool call does not wait
        # for a cold index. A cached index is already warm by this point.
        client.load_cache()
        yield {"client": client}
    finally:
        await client.close()


mcp = FastMCP(
    "Joplin",
    instructions=(
        "MCP server for managing notes, notebooks, tags, and attachments on a "
        "Joplin Server.\n\n"
        "Partial edits: prefer append_to_note, replace_in_note, and "
        "replace_section over update_note. They splice the edit server-side, so "
        "you send only the fragment that changes and existing content -- "
        "including resource links -- is preserved byte for byte. Use "
        "get_note_outline to find a section by line number, get_note(raw=True) "
        "to copy an exact anchor, and dry_run=True to preview any write. Every "
        "edit reports character/line/heading deltas plus the lines around the "
        "change so you can verify it without re-reading the note."
    ),
    lifespan=lifespan,
)


def get_client(ctx: Context) -> JoplinClient:
    """Retrieve the shared JoplinClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


# -- Connection --------------------------------------------------------------


@mcp.tool()
async def ping_joplin(ctx: Context) -> PingResponse:
    """Check connectivity to Joplin Server and report what the index can see."""
    return await get_client(ctx).ping()


# -- Notebook tools ----------------------------------------------------------


@mcp.tool()
async def list_notebooks(ctx: Context) -> list[NotebookSummary]:
    """List all notebooks, with their IDs and parent notebooks."""
    return await get_client(ctx).list_notebooks()


@mcp.tool()
async def get_notebook(ctx: Context, notebook_id: str) -> NotebookDetail | JoplinError:
    """Get a notebook with its notes and sub-notebooks.

    Args:
        notebook_id: The 32-character hex notebook ID.
    """
    return await get_client(ctx).get_notebook(notebook_id)


@mcp.tool()
async def create_notebook(
    ctx: Context,
    title: str,
    parent_id: str = "",
) -> NotebookCreatedResponse | JoplinError:
    """Create a new notebook (folder).

    Args:
        title: Notebook title.
        parent_id: Parent notebook ID for nesting (optional). Defaults to the
            configured root notebook when JOPLIN_NOTEBOOK_ID is set.
    """
    return await get_client(ctx).create_notebook(title=title, parent_id=parent_id)


@mcp.tool()
async def get_or_create_notebook(
    ctx: Context, path: str
) -> NotebookPathResult | JoplinError:
    """Resolve a "/"-separated notebook path, creating any missing levels.

    Args:
        path: Notebook path, e.g. "Work/Projects/Website".
    """
    return await get_client(ctx).get_or_create_notebook(path)


@mcp.tool()
async def update_notebook(
    ctx: Context,
    notebook_id: str,
    title: str | None = None,
    parent_id: str | None = None,
) -> NotebookUpdatedResponse | JoplinError:
    """Rename a notebook or move it to a different parent.

    A move that would create a circular reference is refused, as is a move
    inside the notebook's own subtree.

    Args:
        notebook_id: The notebook ID to update.
        title: New title (optional).
        parent_id: New parent notebook ID, empty string for root (optional).
    """
    return await get_client(ctx).update_notebook(
        notebook_id=notebook_id, title=title, parent_id=parent_id
    )


@mcp.tool()
async def delete_notebook(
    ctx: Context,
    notebook_id: str,
    force: bool = False,
) -> NotebookDeletedResponse | JoplinError:
    """Delete a notebook. Refuses a non-empty notebook unless force=True.

    With force=True the notebook's entire subtree -- notes and nested
    sub-notebooks at any depth -- is deleted.

    Args:
        notebook_id: The notebook ID to delete.
        force: Delete with all contents if True.
    """
    return await get_client(ctx).delete_notebook(notebook_id=notebook_id, force=force)


# -- Note tools --------------------------------------------------------------


@mcp.tool()
async def list_notes(
    ctx: Context,
    notebook_id: str | None = None,
    limit: int = 50,
    tag: str | None = None,
    todo: str | None = None,
) -> list[NoteSummary] | JoplinError:
    """List notes, optionally filtered by notebook, tag, and to-do state.

    Args:
        notebook_id: Filter by notebook ID (optional).
        limit: Maximum number of notes to return (default 50).
        tag: Filter by tag name (optional).
        todo: Filter by to-do state: 'all' (any to-do), 'open' (uncompleted),
            'done' (completed), or omit for no filtering (optional).
    """
    return await get_client(ctx).list_notes(
        notebook_id=notebook_id, limit=limit, tag=tag, todo=todo
    )


@mcp.tool()
async def get_all_notes(
    ctx: Context,
    notebook_id: str | None = None,
    order_by: str = "updated_time",
    order_dir: str = "desc",
    page: int = 1,
    limit: int = 50,
    todo: str | None = None,
) -> NotePage | JoplinError:
    """Get all notes with pagination, sorting, and optional filters.

    Args:
        notebook_id: Filter by notebook ID (optional).
        order_by: Sort field: updated_time, created_time, title, todo_due, or
            todo_completed (default: updated_time). Unset to-do dates sort last.
        order_dir: Sort direction: 'asc' or 'desc' (default: desc).
        page: Page number starting from 1 (default: 1).
        limit: Notes per page, max 100 (default: 50).
        todo: Filter by to-do state: 'all', 'open', 'done', or omit for no
            filtering (optional).
    """
    return await get_client(ctx).get_all_notes(
        notebook_id=notebook_id,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
        limit=limit,
        todo=todo,
    )


@mcp.tool()
async def search_notes(
    ctx: Context,
    query: str,
    limit: int = 20,
    scope: str = "all",
    notebook_id: str | None = None,
    tag: str | None = None,
) -> list[NoteSummary] | JoplinError:
    """Search notes by text. All query terms must match (AND).

    Args:
        query: Space-separated terms; a note must contain all of them.
        limit: Maximum number of results (default 20).
        scope: Where to match: 'title', 'body', or 'all' (default).
        notebook_id: Restrict to a notebook (optional).
        tag: Restrict to notes carrying this tag name (optional).
    """
    return await get_client(ctx).search_notes(
        query=query, limit=limit, scope=scope, notebook_id=notebook_id, tag=tag
    )


@mcp.tool()
async def get_note(
    ctx: Context, note_id: str, raw: bool = False
) -> NoteDetail | JoplinError:
    """Get a note's content.

    Args:
        note_id: The 32-character hex note ID.
        raw: Return the body verbatim -- no resource labels substituted, so it
            can be copied as an exact anchor for replace_in_note (default False).
    """
    return await get_client(ctx).get_note(note_id, raw=raw)


@mcp.tool()
async def get_notes_batch(
    ctx: Context, note_ids: list[str], raw: bool = False
) -> NoteBatch | JoplinError:
    """Read several notes at once, fetched in parallel.

    Args:
        note_ids: Note IDs to read, up to 50.
        raw: Return each body verbatim (default False).
    """
    return await get_client(ctx).get_notes_batch(note_ids, raw=raw)


@mcp.tool()
async def get_note_full(ctx: Context, note_id: str) -> NoteWithResources | JoplinError:
    """Get a note with all its resources embedded as base64. Can be large.

    Args:
        note_id: The 32-character hex note ID.
    """
    return await get_client(ctx).get_note_full(note_id)


@mcp.tool()
async def export_note(ctx: Context, note_id: str) -> NoteExport | JoplinError:
    """Export a note as markdown with resources as base64 blocks.

    The body uses plain local filenames (e.g. ![](photo.jpg)) and each resource
    is returned separately with its filename, MIME type and base64 payload, ready
    to be written to disk.

    Args:
        note_id: The 32-character hex note ID.
    """
    return await get_client(ctx).export_note(note_id)


@mcp.tool()
async def create_note(
    ctx: Context,
    title: str,
    body: str = "",
    notebook_id: str = "",
    is_todo: bool = False,
    due: str | None = None,
) -> NoteCreatedResponse | JoplinError:
    """Create a new note.

    Args:
        title: Note title.
        body: Note body in Markdown.
        notebook_id: Parent notebook ID (optional).
        is_todo: Create the note as a to-do (optional).
        due: Due date as YYYY-MM-DD or an ISO-8601 timestamp. Setting it implies
            is_todo; bare dates are UTC midnight (optional).
    """
    return await get_client(ctx).create_note(
        title=title, body=body, notebook_id=notebook_id, is_todo=is_todo, due=due
    )


@mcp.tool()
async def update_note(
    ctx: Context,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    notebook_id: str | None = None,
) -> NoteUpdatedResponse | JoplinError:
    """Replace a note's title, body, and/or notebook in one write.

    `body` must be the complete new text, which makes this a poor fit for large
    notes. For anything short of a full rewrite prefer append_to_note,
    replace_in_note, or replace_section: they splice server-side and never make
    you reproduce existing content.

    Args:
        note_id: The note ID to update.
        title: New title (optional).
        body: New body -- replaces the entire body (optional).
        notebook_id: Move to another notebook (optional).
    """
    return await get_client(ctx).update_note(
        note_id=note_id, title=title, body=body, notebook_id=notebook_id
    )


@mcp.tool()
async def get_note_outline(ctx: Context, note_id: str) -> NoteOutline | JoplinError:
    """Map a note's structure: headings with line numbers and sizes.

    A cheap way to navigate a large note before editing it -- pick a heading
    from here and hand it to append_to_note or replace_section instead of
    reading the whole body. Sizes cover each heading's span, subsections
    included. No body text is returned.

    Args:
        note_id: The 32-character hex note ID.
    """
    return await get_client(ctx).get_note_outline(note_id)


@mcp.tool()
async def append_to_note(
    ctx: Context,
    note_id: str,
    text: str,
    section: str | None = None,
    position: str = "end",
    separator: str = "\n\n",
    if_absent: str | None = None,
    dry_run: bool = False,
) -> EditReport | JoplinError:
    """Add text to a note without resending its existing body.

    The note is read and spliced server-side, so everything already there --
    including resource links -- is preserved byte for byte. Prefer this over
    update_note whenever you are only adding content.

    Args:
        note_id: The note ID.
        text: Markdown to insert.
        section: Heading to insert under, e.g. "Hosts" or "## Hosts". Applies to
            the whole note when omitted (optional).
        position: Where the text goes.
            Inside the section (or note): "end" (default) or "start".
            Beside a named section, as a sibling: "before" its heading, or
            "after" its whole span -- subsections included. These two require
            `section`, ignore `separator`, and never rewrite an existing byte,
            which makes "before" the way to put a new entry at the top of a
            newest-first log that opens with a preamble.
        separator: Text placed between existing content and the insert for
            "start"/"end" (default: a blank line; pass "\\n" for table rows or
            list items). Ignored by "before"/"after".
        if_absent: Skip the write when this string is already in the body -- an
            idempotency guard that makes a re-run a no-op (optional).
        dry_run: Report the change and its context without writing.
    """
    return await get_client(ctx).append_to_note(
        note_id=note_id,
        text=text,
        section=section,
        position=position,
        separator=separator,
        if_absent=if_absent,
        dry_run=dry_run,
    )


@mcp.tool()
async def replace_in_note(
    ctx: Context,
    note_id: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    dry_run: bool = False,
) -> EditReport | JoplinError:
    """Replace an exact string inside a note body -- a surgical edit.

    Only the fragment crosses the wire; the rest of the note is never rewritten.
    Refuses to act when `old_text` is missing, or matches more than once without
    replace_all, so a bad anchor cannot silently mangle a note. Pass an empty
    `new_text` to delete the fragment.

    Args:
        note_id: The note ID.
        old_text: Exact text to find -- copy it verbatim from get_note(raw=True).
        new_text: Replacement text ("" deletes the anchor).
        replace_all: Replace every occurrence instead of demanding a unique
            match.
        dry_run: Report the change and its context without writing.
    """
    return await get_client(ctx).replace_in_note(
        note_id=note_id,
        old_text=old_text,
        new_text=new_text,
        replace_all=replace_all,
        dry_run=dry_run,
    )


@mcp.tool()
async def replace_section(
    ctx: Context,
    note_id: str,
    section: str,
    text: str,
    dry_run: bool = False,
) -> EditReport | JoplinError:
    """Replace everything under a heading, keeping the heading line itself.

    Rewrites one section of a large note without resending -- or even reading --
    the rest of it. Pass an empty `text` to empty the section.

    Args:
        note_id: The note ID.
        section: Heading whose content to replace, e.g. "Hosts" or "## Hosts".
        text: New Markdown content for that section.
        dry_run: Report the change and its context without writing.
    """
    return await get_client(ctx).replace_section(
        note_id=note_id, section=section, text=text, dry_run=dry_run
    )


@mcp.tool()
async def set_todo(
    ctx: Context,
    note_id: str,
    is_todo: bool | None = None,
    completed: bool | None = None,
    due: str | None = None,
) -> TodoUpdateResponse | JoplinError:
    """Set or clear a note's to-do state, completion, and due date.

    Args:
        note_id: The note ID to modify.
        is_todo: Make the note a to-do (True) or a plain note (False). Setting
            False also clears the due date and completion (optional).
        completed: Mark the to-do done (True) or reopen it (False). Completing a
            note also forces it to be a to-do (optional).
        due: Due date as YYYY-MM-DD or an ISO-8601 timestamp; "" clears it.
            Setting a due date also forces the note to be a to-do. Bare dates
            are UTC midnight (optional).
    """
    return await get_client(ctx).set_todo(
        note_id=note_id, is_todo=is_todo, completed=completed, due=due
    )


@mcp.tool()
async def delete_note(ctx: Context, note_id: str) -> NoteDeletedResponse | JoplinError:
    """Delete a note by ID.

    Args:
        note_id: The note ID to delete.
    """
    return await get_client(ctx).delete_note(note_id)


# -- Tag tools ---------------------------------------------------------------


@mcp.tool()
async def list_tags(ctx: Context) -> list[TagSummary]:
    """List all tags on the Joplin server."""
    return await get_client(ctx).list_tags()


@mcp.tool()
async def create_tag(ctx: Context, title: str) -> TagCreatedResponse:
    """Create a new tag.

    Args:
        title: Tag title.
    """
    return await get_client(ctx).create_tag(title)


@mcp.tool()
async def delete_tag(ctx: Context, tag_id: str) -> TagDeletedResponse | JoplinError:
    """Delete a tag and remove it from all notes.

    Args:
        tag_id: The tag ID to delete.
    """
    return await get_client(ctx).delete_tag(tag_id)


@mcp.tool()
async def get_note_tags(ctx: Context, note_id: str) -> list[TagSummary] | JoplinError:
    """List tags assigned to a note.

    Args:
        note_id: The note ID.
    """
    return await get_client(ctx).get_note_tags(note_id)


@mcp.tool()
async def add_tag_to_note(
    ctx: Context, tag_id: str, note_id: str
) -> TagAddedResponse | JoplinError:
    """Add a tag to a note.

    Args:
        tag_id: The tag ID.
        note_id: The note ID.
    """
    return await get_client(ctx).add_tag_to_note(tag_id, note_id)


@mcp.tool()
async def remove_tag_from_note(
    ctx: Context, tag_id: str, note_id: str
) -> TagRemovedResponse | JoplinError:
    """Remove a tag from a note.

    Args:
        tag_id: The tag ID.
        note_id: The note ID.
    """
    return await get_client(ctx).remove_tag_from_note(tag_id, note_id)


# -- Resource tools ----------------------------------------------------------


@mcp.tool()
async def get_note_resources(ctx: Context, note_id: str) -> NoteResources | JoplinError:
    """List resources (images, attachments) referenced by a note.

    Args:
        note_id: The note ID.
    """
    return await get_client(ctx).get_note_resources(note_id)


@mcp.tool()
async def get_resource_info(
    ctx: Context, resource_id: str
) -> ResourceInfo | JoplinError:
    """Get metadata for a resource.

    Args:
        resource_id: The resource ID (32-char hex).
    """
    return await get_client(ctx).get_resource_info(resource_id)


@mcp.tool()
async def download_resource(
    ctx: Context, resource_id: str
) -> ResourceContent | JoplinError:
    """Download a resource as base64. Refuses anything over 50 MB.

    Args:
        resource_id: The resource ID (32-char hex).
    """
    return await get_client(ctx).download_resource(resource_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Joplin MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
