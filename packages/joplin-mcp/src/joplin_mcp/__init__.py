"""Joplin MCP Server.

An MCP server for managing notes, notebooks, and tags on a Joplin Server.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TYPE_NOTE = 1
TYPE_FOLDER = 2
TYPE_TAG = 5
TYPE_NOTE_TAG = 6

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class NotebookSummary(BaseModel):
    """A notebook (folder) on the Joplin server."""

    id: str
    title: str
    parent_id: str = ""


class NoteSummary(BaseModel):
    """Compact note summary for list/search results."""

    id: str
    title: str
    notebook_id: str = ""
    is_todo: bool = False
    updated_time: str = ""
    preview: str = Field(
        default="",
        description="A short extract: load whole note with `get_note` before editing",
    )


class NoteDetail(BaseModel):
    """Full note detail returned by get_note."""

    id: str
    title: str
    body: str = ""
    notebook_id: str = ""
    is_todo: bool = False
    created_time: str = ""
    updated_time: str = ""


class TagSummary(BaseModel):
    """A tag on the Joplin server."""

    id: str
    title: str


class NoteTagLink(BaseModel):
    """A link between a note and a tag."""

    id: str
    note_id: str
    tag_id: str


class NoteCreatedResponse(BaseModel):
    """Response after creating a note."""

    id: str
    message: str


class NoteUpdatedResponse(BaseModel):
    """Response after updating a note."""

    message: str


class NoteDeletedResponse(BaseModel):
    """Response after deleting a note."""

    message: str


class NotebookCreatedResponse(BaseModel):
    """Response after creating a notebook."""

    id: str
    message: str


class NotebookUpdatedResponse(BaseModel):
    """Response after updating a notebook."""

    message: str


class NotebookDeletedResponse(BaseModel):
    """Response after deleting a notebook."""

    message: str


class TagCreatedResponse(BaseModel):
    """Response after creating a tag."""

    id: str
    message: str


class TagDeletedResponse(BaseModel):
    """Response after deleting a tag."""

    message: str


class TagAddedResponse(BaseModel):
    """Response after adding a tag to a note."""

    message: str


class TagRemovedResponse(BaseModel):
    """Response after removing a tag from a note."""

    message: str


class JoplinError(BaseModel):
    """Structured error response."""

    error: str


# ---------------------------------------------------------------------------
# Joplin item parsing helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_joplin_item(raw: str) -> dict[str, Any]:
    """Parse a raw Joplin .md item into its components."""
    lines = raw.split("\n")

    # Find where metadata starts (first line matching "id: <32hex>")
    metadata_start = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^id:\s+[0-9a-f]{32}$", line.strip()):
            metadata_start = i
            break

    # Parse metadata key-value pairs
    metadata: dict[str, str] = {}
    for line in lines[metadata_start:]:
        stripped = line.strip()
        if stripped and ":" in stripped:
            key, _, value = stripped.partition(":")
            metadata[key.strip()] = value.strip()

    # Extract title (first non-empty line before metadata)
    title = ""
    body_start = 0
    for i, line in enumerate(lines[:metadata_start]):
        if line.strip():
            title = line.strip()
            body_start = i + 1
            break

    # Extract body (between title and metadata, trimmed)
    body_lines = lines[body_start:metadata_start]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()

    return {
        "title": title,
        "body": "\n".join(body_lines),
        "id": metadata.get("id", ""),
        "parent_id": metadata.get("parent_id", ""),
        "type": int(metadata.get("type_", "0")),
        "is_todo": metadata.get("is_todo", "0") == "1",
        "created_time": metadata.get("created_time", ""),
        "updated_time": metadata.get("updated_time", ""),
        "metadata": metadata,
    }


def _note_template(
    note_id: str,
    title: str,
    body: str,
    notebook_id: str,
    now: str,
) -> str:
    return f"""{title}

{body}

id: {note_id}
parent_id: {notebook_id}
created_time: {now}
updated_time: {now}
is_conflict: 0
latitude: 0.00000000
longitude: 0.00000000
altitude: 0.0000
author:\x20
source_url:\x20
is_todo: 0
todo_due: 0
todo_completed: 0
source: joplin-mcp
source_application: joplin-mcp
application_data:\x20
order: 0
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text:\x20
encryption_applied: 0
markup_language: 1
is_shared: 0
share_id:\x20
conflict_original_id:\x20
master_key_id:\x20
user_data:\x20
deleted_time: 0
type_: 1"""


def _folder_template(
    folder_id: str,
    title: str,
    parent_id: str,
    now: str,
    share_id: str = "",
) -> str:
    is_shared = "1" if share_id else "0"
    share_id_val = share_id if share_id else "\x20"
    return f"""{title}

id: {folder_id}
parent_id: {parent_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text:\x20
encryption_applied: 0
is_shared: {is_shared}
share_id: {share_id_val}
master_key_id:\x20
icon:\x20
deleted_time: 0
type_: 2"""


def _tag_template(tag_id: str, title: str, now: str) -> str:
    return f"""{title}

id: {tag_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text:\x20
encryption_applied: 0
is_shared: 0
parent_id:\x20
type_: 5"""


def _note_tag_template(
    nt_id: str,
    note_id: str,
    tag_id: str,
    tag_title: str,
    now: str,
) -> str:
    return f"""{tag_title}

id: {nt_id}
note_id: {note_id}
tag_id: {tag_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text:\x20
encryption_applied: 0
is_shared: 0
type_: 6"""


# ---------------------------------------------------------------------------
# Joplin HTTP client
# ---------------------------------------------------------------------------


class JoplinClient:
    """Authenticated client for the Joplin Server REST API."""

    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        root_notebook_id: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.root_notebook_id = root_notebook_id
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                verify=False,  # noqa: S501
                timeout=30,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=60,
                ),
            )
        return self._http

    async def _login(self) -> str:
        http = await self._get_http()
        resp = await http.post(
            f"{self.url}/api/sessions",
            json={"email": self.email, "password": self.password},
        )
        resp.raise_for_status()
        self._session_id = resp.json()["id"]
        log.info("Authenticated with Joplin Server")
        return self._session_id

    async def _get_session(self) -> str:
        if self._session_id:
            return self._session_id
        return await self._login()

    async def _api(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an API request, re-authenticating on 403."""
        token = await self._get_session()
        extra_headers = kwargs.pop("headers", {})
        headers = {"X-API-AUTH": token, **extra_headers}

        http = await self._get_http()
        resp = await http.request(
            method, f"{self.url}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 403:
            self._session_id = None
            token = await self._login()
            headers["X-API-AUTH"] = token
            resp = await http.request(
                method, f"{self.url}{path}", headers=headers, **kwargs
            )
        resp.raise_for_status()
        return resp

    async def _put_item(
        self, item_id: str, content: str, share_id: str = ""
    ) -> None:
        params: dict[str, str] = {}
        if share_id:
            params["share_id"] = share_id
        await self._api(
            "PUT",
            f"/api/items/root:/{item_id}.md:/content",
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
            params=params or None,
        )

    async def _get_share_id(self, notebook_id: str) -> str:
        """Return the share_id of a notebook, walking up the parent chain."""
        if not notebook_id or not _ID_RE.match(notebook_id):
            return ""
        try:
            resp = await self._api("GET", f"/api/items/root:/{notebook_id}.md:/content")
            parsed = _parse_joplin_item(resp.text)
            share_id = parsed["metadata"].get("share_id", "").strip()
            if share_id:
                return share_id
            parent = parsed.get("parent_id", "")
            if parent and _ID_RE.match(parent):
                return await self._get_share_id(parent)
        except Exception:
            log.debug("Failed to get share_id for %s", notebook_id)
        return ""

    async def _fetch_all_items(self) -> list[dict[str, Any]]:
        """Paginate through all children of the root folder."""
        all_items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._api("GET", "/api/items/root:/:/children", params=params)
            data = resp.json()
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", "")
        return all_items

    async def _fetch_parsed_items(self) -> list[dict[str, Any]]:
        """Fetch and parse all .md items from the server."""
        all_items = await self._fetch_all_items()
        md_names = [
            it["name"] for it in all_items if it.get("name", "").endswith(".md")
        ]
        results: list[dict[str, Any]] = []
        for name in md_names:
            try:
                resp = await self._api("GET", f"/api/items/root:/{name}:/content")
                parsed = _parse_joplin_item(resp.text)
                if parsed["id"]:
                    results.append(parsed)
            except Exception:
                log.debug("Failed to fetch %s", name)
        return results

    async def _get_allowed_notebook_ids(self) -> set[str] | None:
        """Return the set of allowed notebook IDs (root + descendants).

        Returns ``None`` when no restriction is configured.
        """
        if not self.root_notebook_id:
            return None
        items = await self._fetch_parsed_items()
        folders = {
            it["id"]: it["parent_id"]
            for it in items
            if it["type"] == TYPE_FOLDER
        }
        allowed: set[str] = {self.root_notebook_id}
        changed = True
        while changed:
            changed = False
            for fid, pid in folders.items():
                if pid in allowed and fid not in allowed:
                    allowed.add(fid)
                    changed = True
        return allowed

    async def _assert_notebook_allowed(self, notebook_id: str) -> JoplinError | None:
        """Return a ``JoplinError`` if *notebook_id* is outside the allowed tree."""
        allowed = await self._get_allowed_notebook_ids()
        if allowed is not None and notebook_id not in allowed:
            return JoplinError(
                error=f"Notebook {notebook_id} is outside the configured root notebook"
            )
        return None

    # -- Notebooks ----------------------------------------------------------

    async def list_notebooks(self) -> list[NotebookSummary]:
        """List all notebooks (filtered to allowed tree if configured)."""
        items = await self._fetch_parsed_items()
        allowed = await self._get_allowed_notebook_ids()
        return [
            NotebookSummary(
                id=it["id"],
                title=it["title"],
                parent_id=it["parent_id"],
            )
            for it in items
            if it["type"] == TYPE_FOLDER
            and (allowed is None or it["id"] in allowed)
        ]

    async def get_notebook(self, notebook_id: str) -> NotebookSummary | JoplinError:
        """Get a single notebook by ID."""
        if not _ID_RE.match(notebook_id):
            return JoplinError(error=f"Invalid notebook ID: '{notebook_id}'")
        err = await self._assert_notebook_allowed(notebook_id)
        if err:
            return err
        try:
            resp = await self._api("GET", f"/api/items/root:/{notebook_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Notebook {notebook_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_FOLDER:
            return JoplinError(error=f"Item {notebook_id} is not a notebook")
        return NotebookSummary(
            id=parsed["id"],
            title=parsed["title"],
            parent_id=parsed["parent_id"],
        )

    async def create_notebook(
        self, title: str, parent_id: str = ""
    ) -> NotebookCreatedResponse | JoplinError:
        """Create a new notebook."""
        if self.root_notebook_id:
            if not parent_id:
                parent_id = self.root_notebook_id
            err = await self._assert_notebook_allowed(parent_id)
            if err:
                return err
        nb_id = uuid.uuid4().hex
        now = _now_iso()
        share_id = await self._get_share_id(parent_id) if parent_id else ""
        await self._put_item(
            nb_id,
            _folder_template(nb_id, title, parent_id, now, share_id=share_id),
            share_id=share_id,
        )
        return NotebookCreatedResponse(
            id=nb_id, message=f"Notebook '{title}' created successfully"
        )

    async def update_notebook(
        self,
        notebook_id: str,
        title: str | None = None,
        parent_id: str | None = None,
    ) -> NotebookUpdatedResponse | JoplinError:
        """Update a notebook's title or parent."""
        if not _ID_RE.match(notebook_id):
            return JoplinError(error=f"Invalid notebook ID: '{notebook_id}'")
        if title is None and parent_id is None:
            return JoplinError(error="Provide at least title or parent_id to update")
        err = await self._assert_notebook_allowed(notebook_id)
        if err:
            return err
        if parent_id is not None and parent_id:
            err = await self._assert_notebook_allowed(parent_id)
            if err:
                return err
        try:
            resp = await self._api("GET", f"/api/items/root:/{notebook_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Notebook {notebook_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_FOLDER:
            return JoplinError(error=f"Item {notebook_id} is not a notebook")

        new_title = title if title is not None else parsed["title"]
        now = _now_iso()
        meta = parsed["metadata"]
        meta["updated_time"] = now
        meta["user_updated_time"] = now
        if parent_id is not None:
            meta["parent_id"] = parent_id

        effective_parent = (
            parent_id if parent_id is not None else meta.get("parent_id", "")
        )
        if not meta.get("share_id", "").strip():
            share_id = await self._get_share_id(effective_parent)
            if share_id:
                meta["share_id"] = share_id
                meta["is_shared"] = "1"

        content = f"{new_title}\n\n" + "\n".join(f"{k}: {v}" for k, v in meta.items())
        share_id = meta.get("share_id", "").strip()
        await self._put_item(notebook_id, content, share_id=share_id)
        return NotebookUpdatedResponse(
            message=f"Notebook {notebook_id} updated successfully"
        )

    async def delete_notebook(
        self, notebook_id: str, force: bool = False
    ) -> NotebookDeletedResponse | JoplinError:
        """Delete a notebook, optionally with all contents."""
        if not _ID_RE.match(notebook_id):
            return JoplinError(error=f"Invalid notebook ID: '{notebook_id}'")
        if notebook_id == self.root_notebook_id:
            return JoplinError(error="Cannot delete the configured root notebook")
        err = await self._assert_notebook_allowed(notebook_id)
        if err:
            return err

        items = await self._fetch_parsed_items()
        nb = None
        for it in items:
            if it["id"] == notebook_id and it["type"] == TYPE_FOLDER:
                nb = it
                break
        if nb is None:
            return JoplinError(error=f"Notebook {notebook_id} not found")

        children = [
            it
            for it in items
            if it["parent_id"] == notebook_id and it["type"] in (TYPE_NOTE, TYPE_FOLDER)
        ]
        if children and not force:
            return JoplinError(
                error=(
                    f"Notebook '{nb['title']}' is not empty "
                    f"({len(children)} items). Set force=True to delete."
                )
            )

        for child in children:
            try:
                await self._api("DELETE", f"/api/items/root:/{child['id']}.md:")
            except Exception:
                log.debug("Failed to delete child %s", child["id"])

        await self._api("DELETE", f"/api/items/root:/{notebook_id}.md:")
        return NotebookDeletedResponse(
            message=f"Notebook '{nb['title']}' deleted successfully"
        )

    # -- Notes --------------------------------------------------------------

    async def list_notes(
        self,
        notebook_id: str | None = None,
        limit: int = 50,
    ) -> list[NoteSummary]:
        """List notes, optionally filtered by notebook."""
        items = await self._fetch_parsed_items()
        allowed = await self._get_allowed_notebook_ids()
        notes = [
            it for it in items
            if it["type"] == TYPE_NOTE
            and (allowed is None or it["parent_id"] in allowed)
        ]
        if notebook_id:
            notes = [n for n in notes if n["parent_id"] == notebook_id]
        notes.sort(key=lambda x: x["updated_time"], reverse=True)
        notes = notes[:limit]
        return [
            NoteSummary(
                id=n["id"],
                title=n["title"],
                notebook_id=n["parent_id"],
                is_todo=n["is_todo"],
                updated_time=n["updated_time"],
                preview=n["body"][:120].replace("\n", " ") if n["body"] else "",
            )
            for n in notes
        ]

    async def search_notes(self, query: str, limit: int = 20) -> list[NoteSummary]:
        """Search notes by text in title or body."""
        q = query.lower()
        items = await self._fetch_parsed_items()
        allowed = await self._get_allowed_notebook_ids()
        notes = [
            it
            for it in items
            if it["type"] == TYPE_NOTE
            and (allowed is None or it["parent_id"] in allowed)
            and (q in it["title"].lower() or q in it["body"].lower())
        ]
        notes.sort(key=lambda x: x["updated_time"], reverse=True)
        notes = notes[:limit]
        return [
            NoteSummary(
                id=n["id"],
                title=n["title"],
                notebook_id=n["parent_id"],
                is_todo=n["is_todo"],
                updated_time=n["updated_time"],
                preview=n["body"][:120].replace("\n", " ") if n["body"] else "",
            )
            for n in notes
        ]

    async def get_note(self, note_id: str) -> NoteDetail | JoplinError:
        """Get full details of a single note by ID."""
        if not _ID_RE.match(note_id):
            return JoplinError(error=f"Invalid note ID: '{note_id}'")
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note")
        err = await self._assert_notebook_allowed(parsed["parent_id"])
        if err:
            return err
        return NoteDetail(
            id=parsed["id"],
            title=parsed["title"],
            body=parsed["body"],
            notebook_id=parsed["parent_id"],
            is_todo=parsed["is_todo"],
            created_time=parsed["created_time"],
            updated_time=parsed["updated_time"],
        )

    async def create_note(
        self,
        title: str,
        body: str = "",
        notebook_id: str = "",
    ) -> NoteCreatedResponse | JoplinError:
        """Create a new note.

        If *notebook_id* is not provided and a root notebook is configured, the
        note is placed in the root notebook.  Otherwise, if exactly one notebook
        exists, the note is placed there automatically.
        """
        if not notebook_id:
            if self.root_notebook_id:
                notebook_id = self.root_notebook_id
            else:
                notebooks = await self.list_notebooks()
                if len(notebooks) == 1:
                    notebook_id = notebooks[0].id
        if notebook_id:
            err = await self._assert_notebook_allowed(notebook_id)
            if err:
                return err
        note_id = uuid.uuid4().hex
        now = _now_iso()
        share_id = await self._get_share_id(notebook_id) if notebook_id else ""
        await self._put_item(
            note_id,
            _note_template(note_id, title, body, notebook_id, now),
            share_id=share_id,
        )
        return NoteCreatedResponse(
            id=note_id, message=f"Note '{title}' created successfully"
        )

    async def update_note(
        self,
        note_id: str,
        title: str | None = None,
        body: str | None = None,
        notebook_id: str | None = None,
    ) -> NoteUpdatedResponse | JoplinError:
        """Update an existing note. Only provided fields are changed."""
        if not _ID_RE.match(note_id):
            return JoplinError(error=f"Invalid note ID: '{note_id}'")
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note")
        err = await self._assert_notebook_allowed(parsed["parent_id"])
        if err:
            return err
        if notebook_id is not None:
            err = await self._assert_notebook_allowed(notebook_id)
            if err:
                return err

        new_title = title if title is not None else parsed["title"]
        new_body = body if body is not None else parsed["body"]
        now = _now_iso()

        meta = parsed["metadata"]
        meta["updated_time"] = now
        meta["user_updated_time"] = now
        if notebook_id is not None:
            meta["parent_id"] = notebook_id

        effective_parent = (
            notebook_id if notebook_id is not None else meta.get("parent_id", "")
        )
        share_id = await self._get_share_id(effective_parent) if effective_parent else ""

        content = f"{new_title}\n\n{new_body}\n\n" + "\n".join(
            f"{k}: {v}" for k, v in meta.items()
        )
        await self._put_item(note_id, content, share_id=share_id)
        return NoteUpdatedResponse(message=f"Note {note_id} updated successfully")

    async def edit_note(
        self,
        note_id: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> NoteUpdatedResponse | JoplinError:
        """Edit a note by replacing occurrences of old_string with new_string.

        Reads the note first, performs the replacement on the body, then saves.
        """
        if not _ID_RE.match(note_id):
            return JoplinError(error=f"Invalid note ID: '{note_id}'")
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note")
        err = await self._assert_notebook_allowed(parsed["parent_id"])
        if err:
            return err

        old_body = parsed["body"]
        if old_string not in old_body:
            return JoplinError(error=f"old_string not found in note {note_id} body")

        if replace_all:
            new_body = old_body.replace(old_string, new_string)
        else:
            new_body = old_body.replace(old_string, new_string, 1)

        now = _now_iso()
        meta = parsed["metadata"]
        meta["updated_time"] = now
        meta["user_updated_time"] = now

        parent_id = meta.get("parent_id", "")
        share_id = await self._get_share_id(parent_id) if parent_id else ""

        content = f"{parsed['title']}\n\n{new_body}\n\n" + "\n".join(
            f"{k}: {v}" for k, v in meta.items()
        )
        await self._put_item(note_id, content, share_id=share_id)
        return NoteUpdatedResponse(message=f"Note {note_id} edited successfully")

    async def delete_note(self, note_id: str) -> NoteDeletedResponse | JoplinError:
        """Delete a note by ID."""
        if not _ID_RE.match(note_id):
            return JoplinError(error=f"Invalid note ID: '{note_id}'")
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note")
        err = await self._assert_notebook_allowed(parsed["parent_id"])
        if err:
            return err

        await self._api("DELETE", f"/api/items/root:/{note_id}.md:")
        return NoteDeletedResponse(
            message=f"Note '{parsed['title']}' deleted successfully"
        )

    # -- Tags ---------------------------------------------------------------

    async def list_tags(self) -> list[TagSummary]:
        """List all tags."""
        items = await self._fetch_parsed_items()
        return [
            TagSummary(id=it["id"], title=it["title"])
            for it in items
            if it["type"] == TYPE_TAG
        ]

    async def create_tag(self, title: str) -> TagCreatedResponse:
        """Create a new tag."""
        tag_id = uuid.uuid4().hex
        now = _now_iso()
        await self._put_item(tag_id, _tag_template(tag_id, title, now))
        return TagCreatedResponse(
            id=tag_id, message=f"Tag '{title}' created successfully"
        )

    async def delete_tag(self, tag_id: str) -> TagDeletedResponse | JoplinError:
        """Delete a tag and remove all its note associations."""
        if not _ID_RE.match(tag_id):
            return JoplinError(error=f"Invalid tag ID: '{tag_id}'")

        items = await self._fetch_parsed_items()
        tag = None
        for it in items:
            if it["id"] == tag_id and it["type"] == TYPE_TAG:
                tag = it
                break
        if tag is None:
            return JoplinError(error=f"Tag {tag_id} not found")

        # Remove note-tag associations
        note_tags = [
            it
            for it in items
            if it["type"] == TYPE_NOTE_TAG and it["metadata"].get("tag_id") == tag_id
        ]
        for nt in note_tags:
            try:
                await self._api("DELETE", f"/api/items/root:/{nt['id']}.md:")
            except Exception:
                log.debug("Failed to delete note-tag %s", nt["id"])

        await self._api("DELETE", f"/api/items/root:/{tag_id}.md:")
        return TagDeletedResponse(
            message=(
                f"Tag '{tag['title']}' deleted successfully "
                f"(removed from {len(note_tags)} notes)"
            )
        )

    async def get_note_tags(self, note_id: str) -> list[TagSummary] | JoplinError:
        """List tags assigned to a note."""
        if not _ID_RE.match(note_id):
            return JoplinError(error=f"Invalid note ID: '{note_id}'")
        items = await self._fetch_parsed_items()
        note = None
        for it in items:
            if it["id"] == note_id and it["type"] == TYPE_NOTE:
                note = it
                break
        if note is None:
            return JoplinError(error=f"Note {note_id} not found")
        err = await self._assert_notebook_allowed(note.get("parent_id", ""))
        if err:
            return err

        tag_ids = {
            it["metadata"]["tag_id"]
            for it in items
            if it["type"] == TYPE_NOTE_TAG and it["metadata"].get("note_id") == note_id
        }
        tags_by_id = {it["id"]: it for it in items if it["type"] == TYPE_TAG}
        return [
            TagSummary(
                id=tid,
                title=tags_by_id[tid]["title"] if tid in tags_by_id else tid,
            )
            for tid in sorted(tag_ids)
        ]

    async def add_tag_to_note(
        self, tag_id: str, note_id: str
    ) -> TagAddedResponse | JoplinError:
        """Add a tag to a note."""
        for val, label in [(tag_id, "tag ID"), (note_id, "note ID")]:
            if not _ID_RE.match(val):
                return JoplinError(error=f"Invalid {label}: '{val}'")

        items = await self._fetch_parsed_items()
        tag = None
        note = None
        for it in items:
            if it["id"] == tag_id and it["type"] == TYPE_TAG:
                tag = it
            if it["id"] == note_id and it["type"] == TYPE_NOTE:
                note = it
        if tag is None:
            return JoplinError(error=f"Tag {tag_id} not found")
        if note is None:
            return JoplinError(error=f"Note {note_id} not found")
        err = await self._assert_notebook_allowed(note.get("parent_id", ""))
        if err:
            return err

        # Check if already linked
        already = any(
            it["type"] == TYPE_NOTE_TAG
            and it["metadata"].get("note_id") == note_id
            and it["metadata"].get("tag_id") == tag_id
            for it in items
        )
        if already:
            return JoplinError(
                error=f"Tag '{tag['title']}' already on note '{note['title']}'"
            )

        nt_id = uuid.uuid4().hex
        now = _now_iso()
        note_parent = note.get("parent_id", "")
        share_id = await self._get_share_id(note_parent) if note_parent else ""
        await self._put_item(
            nt_id,
            _note_tag_template(nt_id, note_id, tag_id, tag["title"], now),
            share_id=share_id,
        )
        return TagAddedResponse(
            message=(f"Tag '{tag['title']}' added to note '{note['title']}'")
        )

    async def remove_tag_from_note(
        self, tag_id: str, note_id: str
    ) -> TagRemovedResponse | JoplinError:
        """Remove a tag from a note."""
        for val, label in [(tag_id, "tag ID"), (note_id, "note ID")]:
            if not _ID_RE.match(val):
                return JoplinError(error=f"Invalid {label}: '{val}'")

        items = await self._fetch_parsed_items()

        # Verify the note is in an allowed notebook
        note = next(
            (it for it in items if it["id"] == note_id and it["type"] == TYPE_NOTE),
            None,
        )
        if note is None:
            return JoplinError(error=f"Note {note_id} not found")
        err = await self._assert_notebook_allowed(note.get("parent_id", ""))
        if err:
            return err

        note_tags = [
            it
            for it in items
            if it["type"] == TYPE_NOTE_TAG
            and it["metadata"].get("note_id") == note_id
            and it["metadata"].get("tag_id") == tag_id
        ]
        if not note_tags:
            return JoplinError(error="Tag is not assigned to this note")

        for nt in note_tags:
            await self._api("DELETE", f"/api/items/root:/{nt['id']}.md:")

        return TagRemovedResponse(message="Tag removed from note successfully")

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# ---------------------------------------------------------------------------
# MCP Server
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
        yield {"client": client}
    finally:
        await client.close()


mcp = FastMCP(
    "Joplin",
    instructions=(
        "MCP server for managing notes, notebooks, and tags on a Joplin Server. "
        "Use the provided tools to list, search, create, update, edit, and delete "
        "notes and notebooks, as well as manage tags."
    ),
    lifespan=lifespan,
)


def _get_client(ctx: Context) -> JoplinClient:
    """Retrieve the shared JoplinClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


# -- Notebook tools ---------------------------------------------------------


@mcp.tool()
async def list_notebooks(ctx: Context) -> list[NotebookSummary]:
    """List all notebooks on the Joplin server."""
    client = _get_client(ctx)
    return await client.list_notebooks()


@mcp.tool()
async def get_notebook(ctx: Context, notebook_id: str) -> NotebookSummary | JoplinError:
    """Get details of a single notebook.

    Args:
        notebook_id: The 32-character hex notebook ID.
    """
    client = _get_client(ctx)
    return await client.get_notebook(notebook_id)


@mcp.tool()
async def create_notebook(
    ctx: Context,
    title: str,
    parent_id: str = "",
) -> NotebookCreatedResponse | JoplinError:
    """Create a new notebook (folder).

    Args:
        title: Notebook title.
        parent_id: Parent notebook ID for nesting (optional).
    """
    client = _get_client(ctx)
    return await client.create_notebook(title=title, parent_id=parent_id)


@mcp.tool()
async def update_notebook(
    ctx: Context,
    notebook_id: str,
    title: str | None = None,
    parent_id: str | None = None,
) -> NotebookUpdatedResponse | JoplinError:
    """Update a notebook's title or parent. Only provided fields are changed.

    Args:
        notebook_id: The notebook ID to update.
        title: New title (optional).
        parent_id: New parent notebook ID, empty string for root (optional).
    """
    client = _get_client(ctx)
    return await client.update_notebook(
        notebook_id=notebook_id, title=title, parent_id=parent_id
    )


@mcp.tool()
async def delete_notebook(
    ctx: Context,
    notebook_id: str,
    force: bool = False,
) -> NotebookDeletedResponse | JoplinError:
    """Delete a notebook. Refuses if non-empty unless force=True.

    Args:
        notebook_id: The notebook ID to delete.
        force: Delete with all contents if True.
    """
    client = _get_client(ctx)
    return await client.delete_notebook(notebook_id=notebook_id, force=force)


# -- Note tools -------------------------------------------------------------


@mcp.tool()
async def list_notes(
    ctx: Context,
    notebook_id: str | None = None,
    limit: int = 50,
) -> list[NoteSummary]:
    """List notes, optionally filtered by notebook.

    Args:
        notebook_id: Filter by notebook ID (optional).
        limit: Maximum number of notes to return (default 50).
    """
    client = _get_client(ctx)
    return await client.list_notes(notebook_id=notebook_id, limit=limit)


@mcp.tool()
async def search_notes(
    ctx: Context,
    query: str,
    limit: int = 20,
) -> list[NoteSummary]:
    """Search notes by text in title or body.

    Args:
        query: Search string (case-insensitive).
        limit: Maximum number of results (default 20).
    """
    client = _get_client(ctx)
    return await client.search_notes(query=query, limit=limit)


@mcp.tool()
async def get_note(ctx: Context, note_id: str) -> NoteDetail | JoplinError:
    """Get full details of a single note including its body.

    Args:
        note_id: The 32-character hex note ID.
    """
    client = _get_client(ctx)
    return await client.get_note(note_id)


@mcp.tool()
async def create_note(
    ctx: Context,
    title: str,
    body: str = "",
    notebook_id: str = "",
) -> NoteCreatedResponse | JoplinError:
    """Create a new note.

    Args:
        title: Note title.
        body: Note body in Markdown.
        notebook_id: Parent notebook ID (optional).
    """
    client = _get_client(ctx)
    return await client.create_note(title=title, body=body, notebook_id=notebook_id)


@mcp.tool()
async def update_note(
    ctx: Context,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    notebook_id: str | None = None,
) -> NoteUpdatedResponse | JoplinError:
    """Update an existing note. Only provided fields are changed.

    Args:
        note_id: The note ID to update.
        title: New title (optional).
        body: New body in Markdown — replaces entire body (optional).
        notebook_id: Move to another notebook (optional).
    """
    client = _get_client(ctx)
    return await client.update_note(
        note_id=note_id,
        title=title,
        body=body,
        notebook_id=notebook_id,
    )


@mcp.tool()
async def edit_note(
    ctx: Context,
    note_id: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> NoteUpdatedResponse | JoplinError:
    """Edit a note by replacing text in its body.

    Reads the note's current content, replaces occurrences of old_string with
    new_string, and saves the result. Use this for surgical edits instead of
    replacing the entire body.

    Args:
        note_id: The note ID to edit.
        old_string: The exact text to find in the note body.
        new_string: The text to replace it with.
        replace_all: If True, replace all occurrences; otherwise only the first.
    """
    client = _get_client(ctx)
    return await client.edit_note(
        note_id=note_id,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
    )


@mcp.tool()
async def delete_note(ctx: Context, note_id: str) -> NoteDeletedResponse | JoplinError:
    """Delete a note by ID.

    Args:
        note_id: The note ID to delete.
    """
    client = _get_client(ctx)
    return await client.delete_note(note_id)


# -- Tag tools --------------------------------------------------------------


@mcp.tool()
async def list_tags(ctx: Context) -> list[TagSummary]:
    """List all tags on the Joplin server."""
    client = _get_client(ctx)
    return await client.list_tags()


@mcp.tool()
async def create_tag(ctx: Context, title: str) -> TagCreatedResponse:
    """Create a new tag.

    Args:
        title: Tag title.
    """
    client = _get_client(ctx)
    return await client.create_tag(title=title)


@mcp.tool()
async def delete_tag(ctx: Context, tag_id: str) -> TagDeletedResponse | JoplinError:
    """Delete a tag and remove it from all notes.

    Args:
        tag_id: The tag ID to delete.
    """
    client = _get_client(ctx)
    return await client.delete_tag(tag_id)


@mcp.tool()
async def get_note_tags(ctx: Context, note_id: str) -> list[TagSummary] | JoplinError:
    """List tags assigned to a note.

    Args:
        note_id: The note ID.
    """
    client = _get_client(ctx)
    return await client.get_note_tags(note_id)


@mcp.tool()
async def add_tag_to_note(
    ctx: Context, tag_id: str, note_id: str
) -> TagAddedResponse | JoplinError:
    """Add a tag to a note.

    Args:
        tag_id: The tag ID.
        note_id: The note ID.
    """
    client = _get_client(ctx)
    return await client.add_tag_to_note(tag_id=tag_id, note_id=note_id)


@mcp.tool()
async def remove_tag_from_note(
    ctx: Context, tag_id: str, note_id: str
) -> TagRemovedResponse | JoplinError:
    """Remove a tag from a note.

    Args:
        tag_id: The tag ID.
        note_id: The note ID.
    """
    client = _get_client(ctx)
    return await client.remove_tag_from_note(tag_id=tag_id, note_id=note_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Joplin MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
