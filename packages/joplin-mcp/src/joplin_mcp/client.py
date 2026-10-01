"""Authenticated client for the Joplin Server REST API.

The client keeps an in-memory index of every note, notebook, tag and note-tag
item so listing and filtering never re-download the whole account. The index is
refreshed incrementally -- only items whose server-side ``updated_time`` moved are
re-fetched -- behind a short TTL, and can be persisted to disk so a restart
starts warm instead of re-syncing everything.

Body edits are resolved here rather than in the model: a caller sends only the
fragment it wants added or changed, and the surrounding body -- including
``:/<resource-id>`` links, which the read tools render as names -- is preserved
byte for byte.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .editing import (
    Section,
    SectionError,
    context_lines,
    find_section,
    heading_count,
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TYPE_NOTE = 1
TYPE_FOLDER = 2
TYPE_TAG = 5
TYPE_NOTE_TAG = 6
TYPE_RESOURCE = 9
TYPE_REVISION = 13

TYPE_NAMES = {
    TYPE_NOTE: "note",
    TYPE_FOLDER: "notebook",
    TYPE_TAG: "tag",
    TYPE_NOTE_TAG: "note_tag",
    TYPE_RESOURCE: "resource",
    TYPE_REVISION: "revision",
}

#: How long a built index is served before a refresh is triggered.
INDEX_TTL = 120.0
#: Resource metadata changes far less often than note bodies.
RESOURCE_INDEX_TTL = 300.0
#: Parallel item fetches during a sync.
INDEX_CONCURRENCY = 50
#: Refuse to inline a single resource larger than this.
MAX_RESOURCE_SIZE = 50 * 1024 * 1024
#: Notes per ``get_notes_batch`` call.
MAX_BATCH_NOTES = 50

PREVIEW_CHARS = 120

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_META_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:(\s|$)")
_ITEM_ID_LINE_RE = re.compile(r"^id:\s*[0-9a-f]{32}$")
#: The HTML ``<img>`` Joplin writes when a resource is pasted into a note. The
#: resource ID is interpolated at match time, hence the cached factory below.
_IMG_TAG_TEMPLATE = r'<img\s[^>]*src=":/%s"[^>]*/?>'
_RESOURCE_REF_RES = (
    re.compile(r"\(\s*:/([0-9a-f]{32})\s*\)"),
    re.compile(r'src="\s*:/([0-9a-f]{32})"'),
)

#: Signature of the body-edit transforms used by
#: :meth:`JoplinClient._apply_body_edit`: ``body -> (new_body, focus, detail)``.
BodyTransform = Callable[[str], tuple[str, int, str]]


class ItemParseError(ValueError):
    """A Joplin item could not be parsed safely enough to rewrite."""


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------


def _int_or_zero(value: Any) -> int:
    """Tolerant int conversion for Joplin metadata values."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _find_metadata_start(lines: list[str]) -> int | None:
    """Index of the first line of the trailing Joplin metadata block.

    Joplin serialises an item as title, blank line, body, blank line, then a
    metadata block that always opens with ``id:`` and ends with ``type_:``. The
    last line matching that shape wins, so a body which happens to contain
    ``id: <32 hex>`` cannot truncate the note -- a truncation here would turn a
    partial edit into a silent deletion of everything below it.

    Returns ``None`` when no such block exists.
    """
    for start in range(len(lines) - 1, -1, -1):
        if not _ITEM_ID_LINE_RE.match(lines[start].strip()):
            continue
        tail = [line.strip() for line in lines[start:] if line.strip()]
        if not all(_META_LINE_RE.match(line) for line in tail):
            continue
        if any(line.startswith("type_:") for line in tail):
            return start
    return None


def _parse_metadata_block(lines: list[str]) -> dict[str, str]:
    """Parse ``key: value`` lines, tolerating the trailing-space empties."""
    metadata: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not _META_LINE_RE.match(stripped):
            continue
        key, _, value = stripped.partition(":")
        metadata[key.strip()] = value.strip()
    return metadata


def _parse_joplin_item(raw: str, *, strict: bool = False) -> dict[str, Any]:
    """Parse a raw Joplin ``.md`` item into its components.

    With ``strict`` a missing metadata block raises :class:`ItemParseError`
    instead of yielding an item with no id -- the write paths use that so a note
    is never serialised back with its metadata lost.
    """
    lines = raw.split("\n")
    metadata_start = _find_metadata_start(lines)
    if metadata_start is None:
        if strict:
            msg = "Item has no recognisable Joplin metadata block; refusing to rewrite it."
            raise ItemParseError(msg)
        metadata_start = len(lines)

    metadata = _parse_metadata_block(lines[metadata_start:])

    title = ""
    body_start = 0
    for i, line in enumerate(lines[:metadata_start]):
        if line.strip():
            title = line.strip()
            body_start = i + 1
            break

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
        "type": _int_or_zero(metadata.get("type_")),
        "is_todo": metadata.get("is_todo", "0") == "1",
        "todo_due": _int_or_zero(metadata.get("todo_due")),
        "todo_completed": _int_or_zero(metadata.get("todo_completed")),
        "created_time": metadata.get("created_time", ""),
        "updated_time": metadata.get("updated_time", ""),
        "metadata": metadata,
    }


def _parse_resource_metadata(raw: str) -> dict[str, Any]:
    """Parse the metadata block of a ``.resource/<id>.md`` item."""
    lines = raw.split("\n")
    metadata = _parse_metadata_block(lines[1:])
    return {
        "title": lines[0].strip() if lines else "",
        **metadata,
        "size": _int_or_zero(metadata.get("size")),
        "type": _int_or_zero(metadata.get("type_")),
    }


def _find_resource_refs(body: str) -> list[str]:
    """Deduplicated resource IDs referenced by a note body, in order.

    Covers Joplin's markdown form ``(:/<id>)`` and the HTML ``src=":/<id>"``
    attribute produced when a resource is pasted into a note.
    """
    seen: set[str] = set()
    result: list[str] = []
    for pattern in _RESOURCE_REF_RES:
        for ref_id in pattern.findall(body):
            if ref_id not in seen:
                seen.add(ref_id)
                result.append(ref_id)
    return result


@functools.lru_cache(maxsize=256)
def _img_tag_re(resource_id: str) -> re.Pattern[str]:
    """Matcher for the ``<img src=":/<id>">`` tag of one specific resource.

    The pattern is compiled per ID and cached, so repeatedly rendering a note
    that references the same resources costs nothing.
    """
    return re.compile(_IMG_TAG_TEMPLATE % resource_id)


def _serialize_item(title: str, body: str, metadata: dict[str, str]) -> str:
    """Serialise an item back to Joplin's ``.md`` format.

    Metadata key order is preserved, so the written item stays identical apart
    from the fields that actually changed.
    """
    return f"{title}\n\n{body}\n\n" + "\n".join(
        f"{key}: {value}" for key, value in metadata.items()
    )


def _b64(data: bytes) -> str:
    """Base64-encode bytes to ASCII."""
    return base64.b64encode(data).decode("ascii")


def _invalid_id(value: str, label: str = "ID") -> JoplinError:
    """Error for a value that is not a 32-character hex Joplin ID."""
    return JoplinError(
        error=f"Invalid {label}: '{value}'. Must be a 32-character hex string."
    )


def _anchor_hint(body: str, old_text: str) -> str:
    """Explain *why* an anchor missed, when the reason is obvious."""
    stripped = old_text.strip()
    if stripped and stripped in body:
        return (
            " A whitespace-trimmed version does match, so leading or trailing"
            " spaces or newlines differ."
        )
    if "\n" in old_text:
        first = stripped.split("\n")[0].strip()
        if first and first in body:
            return (
                f" Its first line ('{first[:60]}') is present, so the lines"
                " after it differ."
            )
    return ""


# ---------------------------------------------------------------------------
# Item templates
# ---------------------------------------------------------------------------


def _note_template(
    note_id: str,
    title: str,
    body: str,
    notebook_id: str,
    now: str,
    share_id: str = "",
    is_todo: bool = False,
    due_ms: int = 0,
) -> str:
    """A fresh note item with the metadata block Joplin expects."""
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
is_todo: {1 if is_todo else 0}
todo_due: {due_ms}
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
is_shared: {1 if share_id else 0}
share_id: {share_id or "\\x20"}
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
    """A fresh notebook (folder) item."""
    return f"""{title}

id: {folder_id}
parent_id: {parent_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text:\x20
encryption_applied: 0
is_shared: {1 if share_id else 0}
share_id: {share_id or "\\x20"}
master_key_id:\x20
icon:\x20
deleted_time: 0
type_: 2"""


def _tag_template(tag_id: str, title: str, now: str) -> str:
    """A fresh tag item."""
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
    nt_id: str, note_id: str, tag_id: str, tag_title: str, now: str
) -> str:
    """A fresh note-tag link item."""
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


def _index_cache_path(url: str, email: str) -> Path:
    """Per-account index cache file in the temp directory.

    Keyed by server and account so two Joplin servers -- or two users -- never
    read each other's index.
    """
    key = hashlib.sha256(f"{url}|{email}".encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"joplin-mcp-index-{key}.json"


def _cache_file(url: str, email: str) -> Path | None:
    """Resolve the index cache path, or ``None`` when persistence is off.

    ``JOPLIN_INDEX_CACHE_FILE`` overrides the default per-account path; setting
    it to an empty string disables persistence entirely.
    """
    raw = os.environ.get("JOPLIN_INDEX_CACHE_FILE")
    if raw is None:
        return _index_cache_path(url, email)
    if not raw.strip():
        return None
    return Path(raw)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class JoplinClient:
    """Authenticated client for the Joplin Server REST API."""

    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        root_notebook_id: str = "",
        index_ttl: float = INDEX_TTL,
        resource_index_ttl: float = RESOURCE_INDEX_TTL,
    ) -> None:
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.root_notebook_id = root_notebook_id
        self.index_ttl = index_ttl
        self.resource_index_ttl = resource_index_ttl

        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None

        self._index: dict[str, dict[str, Any]] = {}
        self._index_ts: float = 0.0
        self._server_etags: dict[str, str] = {}
        self._pending_writes: set[str] = set()
        self._index_lock: asyncio.Lock | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._cache_loaded = False
        self._cache_path = _cache_file(self.url, self.email)

        self._resource_index: dict[str, dict[str, Any]] = {}
        self._resource_ts: float = 0.0
        self._resource_ready = False
        self._resource_lock: asyncio.Lock | None = None

        self._allowed_ids: set[str] = set()
        self._allowed_ts: float = -1.0

    # -- HTTP & auth --------------------------------------------------------

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                verify=False,  # noqa: S501 - self-hosted servers often use self-signed certs
                timeout=30,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=60,
                ),
            )
        return self._http

    async def login(self) -> str:
        """Start a session and return its ID."""
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
        return await self.login()

    async def _api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an API request, re-authenticating once on 403."""
        token = await self._get_session()
        extra_headers = kwargs.pop("headers", {})
        headers = {"X-API-AUTH": token, **extra_headers}

        http = await self._get_http()
        resp = await http.request(
            method, f"{self.url}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 403:
            self._session_id = None
            token = await self.login()
            headers["X-API-AUTH"] = token
            resp = await http.request(
                method, f"{self.url}{path}", headers=headers, **kwargs
            )
        resp.raise_for_status()
        return resp

    async def close(self) -> None:
        """Cancel any background refresh and close the HTTP client."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # -- Index --------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._index_lock is None:
            self._index_lock = asyncio.Lock()
        return self._index_lock

    def _res_lock(self) -> asyncio.Lock:
        if self._resource_lock is None:
            self._resource_lock = asyncio.Lock()
        return self._resource_lock

    def load_cache(self) -> bool:
        """Populate the index from disk. Returns True if anything was loaded."""
        if self._cache_loaded:
            return bool(self._index)
        self._cache_loaded = True
        if self._cache_path is None or not self._cache_path.exists():
            return False
        try:
            with self._cache_path.open() as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable index cache: %s", exc)
            return False
        index = data.get("index")
        if not isinstance(index, dict):
            return False
        self._index = index
        self._server_etags = data.get("server_etags", {})
        self._index_ts = float(data.get("ts", 0.0))
        self._resource_index = data.get("resource_index", {})
        self._resource_ts = float(data.get("resource_ts", 0.0))
        log.info("Loaded index cache: %d items", len(self._index))
        return bool(self._index)

    def save_cache(self) -> None:
        """Persist the index so a restart starts warm."""
        if self._cache_path is None:
            return
        data = {
            "index": self._index,
            "server_etags": self._server_etags,
            "resource_index": self._resource_index,
            "ts": self._index_ts,
            "resource_ts": self._resource_ts,
        }
        try:
            tmp = self._cache_path.with_suffix(".tmp")
            with tmp.open("w") as handle:
                json.dump(data, handle)
            tmp.replace(self._cache_path)
        except OSError as exc:
            log.warning("Failed to save index cache: %s", exc)

    async def _children(self) -> list[dict[str, Any]]:
        """Paginate through every child item of the root folder."""
        items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._api("GET", "/api/items/root:/:/children", params=params)
            data = resp.json()
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", "")
        return items

    async def _fetch_item(self, name: str) -> dict[str, Any] | None:
        """Fetch and parse one item by its ``<id>.md`` name, revisions excluded."""
        try:
            resp = await self._api("GET", f"/api/items/root:/{name}:/content")
            parsed = _parse_joplin_item(resp.text)
        except Exception as exc:  # noqa: BLE001 - one bad item must not fail a sync
            log.debug("Failed to fetch %s: %s", name, exc)
            return None
        if parsed["type"] == TYPE_REVISION or not parsed["id"]:
            return None
        return parsed

    async def _sync_index(self) -> dict[str, dict[str, Any]]:
        """Refresh the index, re-fetching only items whose ``updated_time`` moved."""
        async with self._lock():
            started = time.monotonic()
            children = await self._children()

            server_ids: set[str] = set()
            stale: list[tuple[str, str, str]] = []
            for child in children:
                name = child.get("name", "")
                # Resources are listed by the same call but tracked separately,
                # under their own ID and TTL; indexing them here as well would
                # make every sync re-fetch them and drop them again.
                if not name.endswith(".md") or name.startswith(".resource/"):
                    continue
                item_id = name[:-3]
                server_ids.add(item_id)
                etag = str(child.get("updated_time", ""))
                if self._server_etags.get(item_id) != etag:
                    stale.append((name, item_id, etag))

            # Drop our own writes the server has confirmed, and any that never
            # made it into the index, so freshly created items are not mistaken
            # for deletions while the listing catches up.
            self._pending_writes.intersection_update(self._index)
            self._pending_writes.difference_update(server_ids)
            deleted = [
                item_id
                for item_id in self._index
                if item_id not in server_ids and item_id not in self._pending_writes
            ]

            if not stale and not deleted:
                self._index_ts = time.time()
                return self._index

            log.info(
                "Syncing index: %d changed, %d deleted (of %d total)",
                len(stale),
                len(deleted),
                len(server_ids),
            )

            semaphore = asyncio.Semaphore(INDEX_CONCURRENCY)

            async def _fetch(name: str) -> dict[str, Any] | None:
                async with semaphore:
                    return await self._fetch_item(name)

            fetched = await asyncio.gather(*(_fetch(name) for name, _, _ in stale))
            for (_, item_id, etag), parsed in zip(stale, fetched):
                self._server_etags[item_id] = etag
                if parsed:
                    self._index[item_id] = parsed
                else:
                    self._index.pop(item_id, None)

            for item_id in deleted:
                self._index.pop(item_id, None)
                self._server_etags.pop(item_id, None)

            self._index_ts = time.time()
            self.save_cache()
            log.info(
                "Index: %d items synced in %.1fs",
                len(self._index),
                time.monotonic() - started,
            )
            return self._index

    async def index(self, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        """Return the index, refreshing in the background once the TTL expires.

        A stale index is served immediately while a refresh runs in the
        background, so reads never block on a sync. ``force_refresh`` awaits the
        sync instead, which the create and delete paths use so they act on
        current server state.
        """
        if not self._cache_loaded:
            self.load_cache()

        if not self._index:
            return await self._sync_index()
        if (time.time() - self._index_ts) < self.index_ttl and not force_refresh:
            return self._index
        if force_refresh:
            return await self._sync_index()

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._sync_index())
        return self._index

    async def _items_of_type(
        self, type_id: int, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        idx = await self.index(force_refresh=force_refresh)
        return [item for item in idx.values() if item["type"] == type_id]

    def _notebook_title(self, notebook_id: str) -> str:
        """Best-effort notebook name from the index; the raw ID when unknown."""
        if not notebook_id:
            return "(root)"
        item = self._index.get(notebook_id)
        return item["title"] if item else notebook_id[:8]

    def _note_tags_map(self) -> dict[str, list[str]]:
        """Map of note ID to its tag titles, built from the index."""
        result: dict[str, list[str]] = {}
        for item in self._index.values():
            if item["type"] != TYPE_NOTE_TAG:
                continue
            note_id = item["metadata"].get("note_id", "")
            tag = self._index.get(item["metadata"].get("tag_id", ""))
            if note_id and tag:
                result.setdefault(note_id, []).append(tag["title"])
        return result

    def _note_summary(
        self, note: dict[str, Any], note_tags: dict[str, list[str]] | None = None
    ) -> NoteSummary:
        """Compact summary for list, search and notebook listings."""
        body = note.get("body") or ""
        due = due_value(note)
        return NoteSummary(
            id=note["id"],
            title=note["title"],
            notebook_id=note["parent_id"],
            notebook_title=self._notebook_title(note["parent_id"]),
            is_todo=bool(note.get("is_todo")),
            todo_marker=todo_marker(note),
            todo_due=ms_to_date(due) if due else "",
            updated_time=note["updated_time"],
            tags=sorted((note_tags or {}).get(note["id"], [])),
            preview=body[:PREVIEW_CHARS].replace("\n", " "),
        )

    def _note_tag_links(
        self, note_id: str | None = None, tag_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Note-tag links filtered by note and/or tag."""
        return [
            item
            for item in self._index.values()
            if item["type"] == TYPE_NOTE_TAG
            and (note_id is None or item["metadata"].get("note_id") == note_id)
            and (tag_id is None or item["metadata"].get("tag_id") == tag_id)
        ]

    # -- Notebook scoping ---------------------------------------------------

    def _allowed_notebook_ids(self) -> set[str] | None:
        """Notebook IDs inside the configured root, or ``None`` when unscoped.

        Recomputed only when the index itself changed, so the scoping check
        costs nothing per call.
        """
        if not self.root_notebook_id:
            return None
        if self._allowed_ts == self._index_ts:
            return self._allowed_ids
        folders = {
            item["id"]: item["parent_id"]
            for item in self._index.values()
            if item["type"] == TYPE_FOLDER
        }
        allowed = {self.root_notebook_id}
        changed = True
        while changed:
            changed = False
            for folder_id, parent_id in folders.items():
                if parent_id in allowed and folder_id not in allowed:
                    allowed.add(folder_id)
                    changed = True
        self._allowed_ids = allowed
        self._allowed_ts = self._index_ts
        return allowed

    def _in_scope(self, parent_id: str) -> bool:
        """True when a notebook ID is reachable from the configured root."""
        allowed = self._allowed_notebook_ids()
        return allowed is None or parent_id in allowed

    async def _note_guard(self, note: dict[str, Any]) -> JoplinError | None:
        """Return an error when a note sits outside the configured root."""
        if not self.root_notebook_id:
            return None
        await self.index()
        if self._in_scope(note["parent_id"]):
            return None
        return JoplinError(
            error=(
                f"Note {note['id']} is outside the configured root notebook"
                f" ({self.root_notebook_id})"
            )
        )

    # -- Writes -------------------------------------------------------------

    async def _put_item(self, item_id: str, content: str, share_id: str = "") -> None:
        """Write an item back and fold it into the index optimistically."""
        params = {"share_id": share_id} if share_id else None
        await self._api(
            "PUT",
            f"/api/items/root:/{item_id}.md:/content",
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
            params=params,
        )
        parsed = _parse_joplin_item(content)
        self._index[parsed["id"] or item_id] = parsed
        self._pending_writes.add(item_id)
        # Force a re-read on the next sync so the index matches the server.
        self._server_etags.pop(item_id, None)

    async def _put_note(
        self, note_id: str, title: str, body: str, meta: dict[str, str]
    ) -> None:
        """Write a note back with refreshed timestamps, preserving key order."""
        now = now_iso()
        meta["updated_time"] = now
        meta["user_updated_time"] = now
        share_id = meta.get("share_id", "").strip()
        await self._put_item(
            note_id, _serialize_item(title, body, meta), share_id=share_id
        )

    async def _delete_item(self, item_id: str) -> None:
        """Delete an item and drop it from the index."""
        await self._api("DELETE", f"/api/items/root:/{item_id}.md:")
        self._index.pop(item_id, None)
        self._server_etags.pop(item_id, None)

    async def _fetch_note(self, note_id: str) -> dict[str, Any]:
        """Fetch a note's current content straight from the server."""
        resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        parsed = _parse_joplin_item(resp.text, strict=True)
        if parsed["type"] != TYPE_NOTE:
            kind = TYPE_NAMES.get(parsed["type"], parsed["type"])
            msg = f"Item {note_id} is not a note (type: {kind})"
            raise ItemParseError(msg)
        guard = await self._note_guard(parsed)
        if guard:
            raise ItemParseError(guard.error)
        return parsed

    async def _get_share_id(self, notebook_id: str) -> str:
        """Share ID of a notebook, walking up the parent chain.

        Joplin propagates a notebook's ``share_id`` to its descendants, so new
        notes and sub-notebooks must inherit it or they stay invisible to the
        other participants of the share.
        """
        if not notebook_id or not _ID_RE.match(notebook_id):
            return ""
        seen: set[str] = set()
        current = notebook_id
        while current and current not in seen:
            seen.add(current)
            cached = self._index.get(current)
            if cached:
                share_id = cached.get("metadata", {}).get("share_id", "").strip()
                current = cached.get("parent_id", "")
                if share_id:
                    return share_id
                continue
            try:
                resp = await self._api("GET", f"/api/items/root:/{current}.md:/content")
                parsed = _parse_joplin_item(resp.text)
            except Exception as exc:  # noqa: BLE001
                log.debug("Failed to resolve share_id for %s: %s", current, exc)
                return ""
            share_id = parsed.get("metadata", {}).get("share_id", "").strip()
            if share_id:
                return share_id
            current = parsed.get("parent_id", "")
        return ""

    # -- Resources ----------------------------------------------------------

    async def _ensure_resource_index(
        self, force: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Build the resource index lazily, on first access."""
        fresh = (
            self._resource_ready
            and (time.time() - self._resource_ts) < self.resource_index_ttl
        )
        if fresh and not force:
            return self._resource_index

        async with self._res_lock():
            fresh = (
                self._resource_ready
                and (time.time() - self._resource_ts) < self.resource_index_ttl
            )
            if fresh and not force:
                return self._resource_index

            started = time.monotonic()
            await self.index()
            names = [
                child["name"]
                for child in await self._children()
                if child.get("name", "").startswith(".resource/")
            ]
            semaphore = asyncio.Semaphore(INDEX_CONCURRENCY)

            async def _fetch(name: str) -> dict[str, Any] | None:
                async with semaphore:
                    try:
                        resp = await self._api(
                            "GET", f"/api/items/root:/{name}:/content"
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Failed to fetch resource %s: %s", name, exc)
                        return None
                    return _parse_resource_metadata(resp.text)

            fetched = await asyncio.gather(*(_fetch(name) for name in names))
            resources: dict[str, dict[str, Any]] = {}
            for parsed in fetched:
                if parsed and parsed.get("id"):
                    resources[parsed["id"]] = parsed
            self._resource_index = resources
            self._resource_ts = time.time()
            self._resource_ready = True
            self.save_cache()
            log.info(
                "Resource index: %d resources in %.1fs",
                len(resources),
                time.monotonic() - started,
            )
            return resources

    def _resource_ref(self, resource_id: str) -> ResourceRef:
        """Metadata for one referenced resource, flagged when unresolved."""
        res = self._resource_index.get(resource_id)
        if not res:
            return ResourceRef(
                id=resource_id, title=f"resource:{resource_id}", resolved=False
            )
        return ResourceRef(
            id=resource_id,
            title=res.get("title", ""),
            mime=res.get("mime", ""),
            size=_int_or_zero(res.get("size")),
        )

    def _render_resource_refs(self, body: str) -> str:
        """Substitute ``:/<id>`` links with readable labels.

        Display only. Writes always operate on the verbatim body, so a label can
        never be written back over a resource link.
        """
        for resource_id in _find_resource_refs(body):
            res = self._resource_index.get(resource_id)
            if res:
                size = _int_or_zero(res.get("size"))
                label = (
                    f"{res.get('title', resource_id)} "
                    f"({res.get('mime', '')}, {size / 1024:.0f}KB)"
                )
            else:
                label = f"resource:{resource_id}"
            body = _img_tag_re(resource_id).sub(f"[{label}]", body)
            body = body.replace(f"(:/{resource_id})", f"({label})")
            body = body.replace(f'src=":/{resource_id}"', f'src="{label}"')
        return body

    def _localize_resource_refs(self, body: str) -> str:
        """Replace resource references with plain local filenames."""
        for resource_id in _find_resource_refs(body):
            res = self._resource_index.get(resource_id)
            filename = res.get("title", "") if res else f"{resource_id}.bin"
            body = _img_tag_re(resource_id).sub(f"![{filename}]({filename})", body)
            body = body.replace(f"(:/{resource_id})", f"({filename})")
        return body

    async def _download_resource(self, resource_id: str) -> tuple[bytes, str, str]:
        """Download a resource; returns ``(bytes, mime, title)``."""
        res = self._resource_index.get(resource_id)
        resp = await self._api(
            "GET", f"/api/items/root:/.resource/{resource_id}:/content"
        )
        return (
            resp.content,
            res.get("mime", "") if res else "application/octet-stream",
            res.get("title", "") if res else resource_id,
        )

    async def _resource_content(self, resource_id: str) -> ResourceContent:
        """Download one resource into a model, capturing per-resource errors."""
        res = self._resource_index.get(resource_id)
        size = _int_or_zero(res.get("size")) if res else 0
        if size > MAX_RESOURCE_SIZE:
            return ResourceContent(
                id=resource_id,
                title=res.get("title", "") if res else "",
                mime=res.get("mime", "") if res else "",
                size=size,
                error=f"Resource too large ({size / 1024 / 1024:.1f} MB, max 50 MB)",
            )
        try:
            data, mime, title = await self._download_resource(resource_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ResourceContent(id=resource_id, error="Resource not found")
            return ResourceContent(id=resource_id, error=str(exc))
        return ResourceContent(
            id=resource_id,
            title=title,
            mime=mime,
            size=len(data),
            base64=_b64(data),
        )

    # -- Body edits ---------------------------------------------------------

    def _build_report(
        self,
        *,
        action: str,
        note: dict[str, Any],
        old_body: str,
        new_body: str,
        focus: int,
        detail: str,
        dry_run: bool,
    ) -> EditReport:
        """Describe a body edit: deltas, the line it landed on, and its context."""
        chars_before, chars_after = len(old_body), len(new_body)
        lines_before, lines_after = line_count(old_body), line_count(new_body)
        heads_before, heads_after = heading_count(old_body), heading_count(new_body)
        changed = new_body != old_body
        line = line_of(new_body, focus)

        if changed:
            message = (
                f"{'Would ' if dry_run else ''}{detail}. "
                f"Chars {chars_before} -> {chars_after} "
                f"({chars_after - chars_before:+}), "
                f"lines {lines_before} -> {lines_after} "
                f"({lines_after - lines_before:+}), "
                f"headings {heads_before} -> {heads_after} "
                f"({heads_after - heads_before:+}); change at line {line}."
            )
        else:
            message = f"No change: {detail}."

        return EditReport(
            id=note["id"],
            title=note["title"],
            action=action,
            dry_run=dry_run,
            changed=changed,
            skipped=not changed,
            detail=detail,
            chars_before=chars_before,
            chars_after=chars_after,
            lines_before=lines_before,
            lines_after=lines_after,
            headings_before=heads_before,
            headings_after=heads_after,
            line=line,
            context_lines=context_lines(new_body if changed else old_body, focus),
            message=message,
        )

    async def _apply_body_edit(
        self, note_id: str, action: str, transform: BodyTransform, dry_run: bool
    ) -> EditReport | JoplinError:
        """Fetch a note, run ``transform``, write, and report.

        ``transform`` returns ``(new_body, focus_offset, detail)``. It raises
        ``ValueError`` to refuse the edit (bad anchor, ambiguous section);
        returning the body unchanged reports a no-op instead of writing.
        """
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        try:
            note = await self._fetch_note(note_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found.")
            raise
        except ItemParseError as exc:
            return JoplinError(error=str(exc))

        old_body = note["body"]
        try:
            new_body, focus, detail = transform(old_body)
        except SectionError as exc:
            return JoplinError(
                error=str(exc),
                hint="Call get_note_outline to list the note's headings.",
            )
        except ValueError as exc:
            return JoplinError(
                error=str(exc),
                hint="Read the note with get_note(raw=True) and copy the anchor verbatim.",
            )

        if new_body != old_body and not dry_run:
            await self._put_note(note_id, note["title"], new_body, note["metadata"])
        return self._build_report(
            action=action,
            note=note,
            old_body=old_body,
            new_body=new_body,
            focus=focus,
            detail=detail,
            dry_run=dry_run,
        )

    async def append_to_note(
        self,
        note_id: str,
        text: str,
        section: str | None = None,
        position: str = "end",
        separator: str = "\n\n",
        if_absent: str | None = None,
        dry_run: bool = False,
    ) -> EditReport | JoplinError:
        """Insert text into a note or section without resending the body."""
        if position not in ("end", "start", "before", "after"):
            return JoplinError(
                error="position must be 'end', 'start', 'before' or 'after'."
            )
        if position in ("before", "after") and not section:
            return JoplinError(
                error=(
                    f"position '{position}' needs a `section` to sit beside — "
                    "for the whole note use 'start' or 'end'."
                )
            )
        if not text:
            return JoplinError(
                error="Nothing to append: `text` is empty.",
                hint="Use replace_in_note to change existing content.",
            )

        def transform(body: str) -> tuple[str, int, str]:
            if if_absent and if_absent in body:
                return body, 0, f"marker '{if_absent[:60]}' already present"
            if position in ("before", "after"):
                target: Section = find_section(body, section or "")
                cut = target.start if position == "before" else target.end
                new_body, at = insert_block(body, cut, text)
                return (
                    new_body,
                    at,
                    f"Inserted {len(text)} chars {position} section {target.label}",
                )
            if section is None:
                start, end, where = 0, len(body), "the note"
            else:
                target = find_section(body, section)
                start, end = target.content_start, target.end
                where = f"section {target.label}"
            new_body, at = splice(body, start, end, text, position, separator)
            return (
                new_body,
                at,
                f"Inserted {len(text)} chars at the {position} of {where}",
            )

        return await self._apply_body_edit(note_id, "append", transform, dry_run)

    async def replace_in_note(
        self,
        note_id: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        dry_run: bool = False,
    ) -> EditReport | JoplinError:
        """Replace an exact, uniquely-identified string inside a note body."""
        if not old_text:
            return JoplinError(
                error="`old_text` must not be empty — use append_to_note to add content."
            )

        def transform(body: str) -> tuple[str, int, str]:
            count = body.count(old_text)
            if count == 0:
                raise ValueError(
                    f"old_text not found in note {note_id}."
                    f"{_anchor_hint(body, old_text)}"
                )
            if count > 1 and not replace_all:
                lines: list[str] = []
                at = -1
                for _ in range(min(count, 10)):
                    at = body.find(old_text, at + 1)
                    lines.append(str(line_of(body, at)))
                raise ValueError(
                    f"old_text matches {count} times (lines {', '.join(lines)}). "
                    "Extend the anchor to make it unique, or pass replace_all=True."
                )

            at = body.find(old_text)
            if replace_all:
                new_body = body.replace(old_text, new_text)
            else:
                new_body = body[:at] + new_text + body[at + len(old_text) :]
            verb = "Deleted" if not new_text else "Replaced"
            hits = count if replace_all else 1
            return (
                new_body,
                at,
                f"{verb} {hits} occurrence(s) of a {len(old_text)}-char anchor",
            )

        return await self._apply_body_edit(note_id, "replace_in", transform, dry_run)

    async def replace_section(
        self, note_id: str, section: str, text: str, dry_run: bool = False
    ) -> EditReport | JoplinError:
        """Replace everything under a heading, keeping the heading line itself."""
        if not section:
            return JoplinError(error="`section` is required.")

        def transform(body: str) -> tuple[str, int, str]:
            target: Section = find_section(body, section)
            before, after = body[: target.content_start], body[target.end :]
            old_len = len(body[target.content_start : target.end].strip("\n"))
            inner = text.strip("\n")
            if inner:
                lead = "\n" if before and not before.endswith("\n\n") else ""
                tail = "\n\n" if after else ""
                new_body = f"{before}{lead}{inner}{tail}{after}"
                at = len(before) + len(lead)
            else:
                gap = "\n" if after else ""
                new_body, at = f"{before}{gap}{after}", len(before)
            return (
                new_body,
                at,
                f"Replaced section {target.label}: {old_len} -> {len(inner)} chars",
            )

        return await self._apply_body_edit(
            note_id, "replace_section", transform, dry_run
        )

    async def set_todo(
        self,
        note_id: str,
        is_todo: bool | None = None,
        completed: bool | None = None,
        due: str | None = None,
    ) -> TodoUpdateResponse | JoplinError:
        """Set or clear a note's to-do state, completion, and due date."""
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        if is_todo is None and completed is None and due is None:
            return JoplinError(
                error="Provide at least one of is_todo, completed, or due to change."
            )

        # Parse the date before touching the note so bad input never lands
        # halfway through a change.
        due_ms: int | None = None
        if due is not None:
            try:
                due_ms = parse_due(due)
            except ValueError as exc:
                return JoplinError(error=f"{exc} Pass '' to clear the date.")

        resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        parsed = _parse_joplin_item(resp.text, strict=True)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note.")
        guard = await self._note_guard(parsed)
        if guard:
            return guard

        meta = parsed["metadata"]
        changes: list[str] = []

        # Metadata values stay strings, the way Joplin serialises them, so the
        # written item and the parsed index agree on types.
        if due_ms is not None:
            meta["todo_due"] = str(due_ms)
            if due_ms:
                meta["is_todo"] = "1"
                changes.append(f"due set to {ms_to_date(due_ms)}")
            else:
                changes.append("due cleared")

        if completed is not None:
            if completed:
                done_ms = now_ms()
                meta["todo_completed"] = str(done_ms)
                meta["is_todo"] = "1"
                changes.append(f"marked done ({ms_to_date(done_ms)})")
            else:
                meta["todo_completed"] = "0"
                changes.append("reopened")

        # Applied last so converting to a plain note overrides the flags above.
        if is_todo is not None:
            if is_todo:
                meta["is_todo"] = "1"
                changes.append("marked as to-do")
            else:
                meta["is_todo"] = "0"
                meta["todo_due"] = "0"
                meta["todo_completed"] = "0"
                changes.append("converted to plain note")

        await self._put_note(note_id, parsed["title"], parsed["body"], meta)
        done_ms = _int_or_zero(meta.get("todo_completed"))
        due_stamp = _int_or_zero(meta.get("todo_due"))
        return TodoUpdateResponse(
            id=note_id,
            title=parsed["title"],
            is_todo=meta.get("is_todo") == "1",
            todo_due=ms_to_date(due_stamp) if due_stamp else "",
            todo_completed=ms_to_date(done_ms) if done_ms else "",
            changes=changes,
            message=f"Note '{parsed['title']}' — {', '.join(changes)}",
        )

    # -- Reading notes ------------------------------------------------------

    async def _load_note(
        self, note_id: str
    ) -> tuple[dict[str, Any] | None, JoplinError | None]:
        """Fetch one note, turning not-found and parse failures into errors."""
        if not _ID_RE.match(note_id):
            return None, _invalid_id(note_id, "note ID")
        await self.index()
        # Resource links are rendered as names, so the metadata index has to be
        # in place before the note is turned into a response.
        await self._ensure_resource_index()
        try:
            return await self._fetch_note(note_id), None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None, JoplinError(error=f"Note {note_id} not found.")
            raise
        except ItemParseError as exc:
            return None, JoplinError(error=str(exc))

    async def get_note(
        self, note_id: str, raw: bool = False
    ) -> NoteDetail | JoplinError:
        """Full detail of one note, optionally with the body verbatim."""
        note, error = await self._load_note(note_id)
        if error:
            return error
        assert note is not None
        return self._to_detail(note, raw)

    def _to_detail(self, note: dict[str, Any], raw: bool = False) -> NoteDetail:
        """Build a NoteDetail, optionally substituting resource labels."""
        body = note["body"] if raw else self._render_resource_refs(note["body"])
        completed = completed_value(note)
        return NoteDetail(
            id=note["id"],
            title=note["title"],
            body=body,
            notebook_id=note["parent_id"],
            notebook_title=self._notebook_title(note["parent_id"]),
            is_todo=bool(note.get("is_todo")),
            todo_marker=todo_marker(note),
            todo_due=ms_to_date(due_value(note)) if due_value(note) else "",
            todo_completed=ms_to_date(completed) if completed else "",
            created_time=note["created_time"],
            updated_time=note["updated_time"],
            raw=raw,
            resource_refs=[
                self._resource_ref(ref) for ref in _find_resource_refs(note["body"])
            ],
        )

    async def get_notes_batch(
        self, note_ids: list[str], raw: bool = False
    ) -> NoteBatch | JoplinError:
        """Read several notes at once, in parallel."""
        if not note_ids:
            return JoplinError(error="No note IDs provided.")
        if len(note_ids) > MAX_BATCH_NOTES:
            return JoplinError(
                error=(
                    f"Too many IDs ({len(note_ids)}); the maximum is {MAX_BATCH_NOTES}."
                )
            )
        invalid = [nid for nid in note_ids if not _ID_RE.match(nid)]
        if invalid:
            return _invalid_id(", ".join(invalid), "note ID")

        await self.index()
        await self._ensure_resource_index()

        async def _read(note_id: str) -> NoteDetail | None:
            try:
                note = await self._fetch_note(note_id)
            except (httpx.HTTPStatusError, ItemParseError) as exc:
                log.debug("Batch read failed for %s: %s", note_id, exc)
                return None
            return self._to_detail(note, raw)

        results = await asyncio.gather(*(_read(nid) for nid in note_ids))
        return NoteBatch(
            notes=[note for note in results if note is not None],
            missing=[nid for nid, note in zip(note_ids, results) if note is None],
        )

    async def get_note_full(self, note_id: str) -> NoteWithResources | JoplinError:
        """A note with every referenced resource inlined as base64."""
        detail = await self.get_note(note_id)
        if isinstance(detail, JoplinError):
            return detail
        resources = await asyncio.gather(
            *(self._resource_content(ref.id) for ref in detail.resource_refs)
        )
        return NoteWithResources(note=detail, resources=list(resources))

    async def export_note(self, note_id: str) -> NoteExport | JoplinError:
        """A note rendered for disk: local filenames plus base64 resources."""
        note, error = await self._load_note(note_id)
        if error:
            return error
        assert note is not None
        await self._ensure_resource_index()

        exported: list[ExportedResource] = []
        for ref in _find_resource_refs(note["body"]):
            content = await self._resource_content(ref)
            if content.error or not content.base64:
                continue
            exported.append(
                ExportedResource(
                    filename=content.title or f"{content.id}.bin",
                    mime=content.mime,
                    size=content.size,
                    base64=content.base64,
                )
            )
        return NoteExport(
            title=note["title"],
            id=note_id,
            body=self._localize_resource_refs(note["body"]),
            resources=exported,
        )

    async def get_note_outline(self, note_id: str) -> NoteOutline | JoplinError:
        """Heading map with line numbers and span sizes -- no body text."""
        note, error = await self._load_note(note_id)
        if error:
            return error
        assert note is not None

        body = note["body"]
        return NoteOutline(
            id=note["id"],
            title=note["title"],
            notebook_id=note["parent_id"],
            notebook_title=self._notebook_title(note["parent_id"]),
            updated_time=note["updated_time"],
            body_chars=len(body),
            body_lines=line_count(body),
            headings=[
                OutlineEntry(
                    level=head.level,
                    title=head.title,
                    line=head.line,
                    chars=head.chars,
                )
                for head in outline(body)
            ],
        )

    # -- Listing & searching ------------------------------------------------

    async def list_notes(
        self,
        notebook_id: str | None = None,
        limit: int = 50,
        tag: str | None = None,
        todo: str | None = None,
    ) -> list[NoteSummary] | JoplinError:
        """List notes, filtered by notebook, tag, and/or to-do state."""
        notes = [
            note
            for note in await self._items_of_type(TYPE_NOTE)
            if self._in_scope(note["parent_id"])
        ]
        if notebook_id:
            notes = [n for n in notes if n["parent_id"] == notebook_id]

        note_tags = self._note_tags_map()
        if tag:
            wanted = tag.strip().lower()
            notes = [
                n
                for n in notes
                if any(t.lower() == wanted for t in note_tags.get(n["id"], []))
            ]

        notes, err = filter_by_todo(notes, todo)
        if err:
            return JoplinError(error=err)

        notes.sort(key=lambda n: n["updated_time"], reverse=True)
        return [self._note_summary(n, note_tags) for n in notes[: max(1, limit)]]

    async def search_notes(
        self,
        query: str,
        limit: int = 20,
        scope: str = "all",
        notebook_id: str | None = None,
        tag: str | None = None,
    ) -> list[NoteSummary] | JoplinError:
        """Search notes; every whitespace-separated term must match."""
        if scope not in ("title", "body", "all"):
            return JoplinError(error="scope must be 'title', 'body', or 'all'.")
        terms = query.lower().split()
        if not terms:
            return []

        notes = [
            note
            for note in await self._items_of_type(TYPE_NOTE)
            if self._in_scope(note["parent_id"])
        ]
        if notebook_id:
            notes = [n for n in notes if n["parent_id"] == notebook_id]

        note_tags = self._note_tags_map()
        if tag:
            wanted = tag.strip().lower()
            notes = [
                n
                for n in notes
                if any(t.lower() == wanted for t in note_tags.get(n["id"], []))
            ]

        def fields(note: dict[str, Any]) -> tuple[str, str]:
            title = note["title"].lower()
            body = (note.get("body") or "").lower()
            if scope == "title":
                return title, ""
            if scope == "body":
                return "", body
            return title, body

        def matches(note: dict[str, Any]) -> bool:
            title, body = fields(note)
            return all(term in title or term in body for term in terms)

        def score(note: dict[str, Any]) -> int:
            title, body = fields(note)
            return sum(
                3 if term in title else 1
                for term in terms
                if term in title or term in body
            )

        matched = [note for note in notes if matches(note)]
        matched.sort(key=lambda n: (score(n), n["updated_time"]), reverse=True)
        return [self._note_summary(n, note_tags) for n in matched[: max(1, limit)]]

    async def get_all_notes(
        self,
        notebook_id: str | None = None,
        order_by: str = "updated_time",
        order_dir: str = "desc",
        page: int = 1,
        limit: int = 50,
        todo: str | None = None,
    ) -> NotePage | JoplinError:
        """Paginated, sorted listing with notebook and to-do filters."""
        allowed_orders = (
            "updated_time",
            "created_time",
            "title",
            "todo_due",
            "todo_completed",
        )
        order_dir = order_dir.lower()
        if order_dir not in ("asc", "desc"):
            return JoplinError(error="order_dir must be 'asc' or 'desc'.")
        if order_by not in allowed_orders:
            return JoplinError(
                error=(
                    "order_by must be 'updated_time', 'created_time', 'title', "
                    "'todo_due', or 'todo_completed'."
                )
            )
        if notebook_id and not _ID_RE.match(notebook_id):
            return _invalid_id(notebook_id, "notebook ID")

        notes = [
            note
            for note in await self._items_of_type(TYPE_NOTE)
            if self._in_scope(note["parent_id"])
        ]
        if notebook_id:
            notebook = self._index.get(notebook_id)
            if not notebook or notebook["type"] != TYPE_FOLDER:
                return JoplinError(error=f"Notebook {notebook_id} not found.")
            notes = [n for n in notes if n["parent_id"] == notebook_id]

        notes, err = filter_by_todo(notes, todo)
        if err:
            return JoplinError(error=err)

        reverse = order_dir == "desc"
        if order_by == "title":
            notes.sort(key=lambda n: n["title"].lower(), reverse=reverse)
        elif order_by in ("todo_due", "todo_completed"):
            value = due_value if order_by == "todo_due" else completed_value
            # Unset (0) values always sort last, whichever direction is asked.
            notes = sorted((n for n in notes if value(n)), key=value, reverse=reverse)
            notes += [n for n in notes if not value(n)]
        else:
            notes.sort(key=lambda n: n.get(order_by, ""), reverse=reverse)

        limit = max(1, min(limit, 100))
        page = max(1, page)
        total = len(notes)
        total_pages = max(1, (total + limit - 1) // limit)
        window = notes[(page - 1) * limit : page * limit]
        note_tags = self._note_tags_map()
        return NotePage(
            notes=[self._note_summary(n, note_tags) for n in window],
            total=total,
            page=page,
            page_size=limit,
            total_pages=total_pages,
            order_by=order_by,
            order_dir=order_dir,
            notebook_id=notebook_id or "",
            notebook_title=self._notebook_title(notebook_id) if notebook_id else "",
        )

    # -- Writing notes ------------------------------------------------------

    async def create_note(
        self,
        title: str,
        body: str = "",
        notebook_id: str = "",
        is_todo: bool = False,
        due: str | None = None,
    ) -> NoteCreatedResponse | JoplinError:
        """Create a note, optionally as a to-do with a due date.

        With no notebook given, the configured root notebook is used, or the
        only notebook on the server when there is exactly one.
        """
        due_ms = 0
        if due:
            try:
                due_ms = parse_due(due)
            except ValueError as exc:
                return JoplinError(error=str(exc))
        is_todo = bool(is_todo or due_ms)

        await self.index()
        if not notebook_id:
            if self.root_notebook_id:
                notebook_id = self.root_notebook_id
            else:
                notebooks = await self.list_notebooks()
                if len(notebooks) == 1:
                    notebook_id = notebooks[0].id
        if notebook_id:
            if not _ID_RE.match(notebook_id):
                return _invalid_id(notebook_id, "notebook ID")
            if not self._in_scope(notebook_id):
                return JoplinError(
                    error=(
                        f"Notebook {notebook_id} is outside the configured root"
                        " notebook"
                    )
                )

        note_id = uuid.uuid4().hex
        share_id = await self._get_share_id(notebook_id) if notebook_id else ""
        await self._put_item(
            note_id,
            _note_template(
                note_id,
                title,
                body,
                notebook_id,
                now_iso(),
                share_id=share_id,
                is_todo=is_todo,
                due_ms=due_ms,
            ),
            share_id=share_id,
        )
        suffix = " (to-do)" if is_todo else ""
        return NoteCreatedResponse(
            id=note_id,
            title=title,
            is_todo=is_todo,
            todo_due=ms_to_date(due_ms) if due_ms else "",
            message=f"Note '{title}' created successfully{suffix} (ID: {note_id})",
        )

    async def update_note(
        self,
        note_id: str,
        title: str | None = None,
        body: str | None = None,
        notebook_id: str | None = None,
    ) -> NoteUpdatedResponse | JoplinError:
        """Replace a note's title, body, and/or notebook.

        ``body`` must be the complete new text. For anything short of a full
        rewrite prefer the partial-edit tools, which resolve the edit server-side
        and never require resending existing content.
        """
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        if title is None and body is None and notebook_id is None:
            return JoplinError(error="Provide at least title, body, or notebook_id.")
        if notebook_id and not _ID_RE.match(notebook_id):
            return _invalid_id(notebook_id, "notebook ID")

        await self.index()
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        parsed = _parse_joplin_item(resp.text, strict=True)
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note.")
        guard = await self._note_guard(parsed)
        if guard:
            return guard
        if notebook_id and not self._in_scope(notebook_id):
            return JoplinError(
                error=f"Notebook {notebook_id} is outside the configured root notebook"
            )

        new_title = title if title is not None else parsed["title"]
        new_body = body if body is not None else parsed["body"]
        meta = parsed["metadata"]
        if notebook_id is not None:
            meta["parent_id"] = notebook_id

        effective_parent = meta.get("parent_id", "")
        if effective_parent and not meta.get("share_id", "").strip():
            share_id = await self._get_share_id(effective_parent)
            if share_id:
                meta["share_id"] = share_id
                meta["is_shared"] = "1"

        await self._put_note(note_id, new_title, new_body, meta)
        changes = []
        if title is not None:
            changes.append("title changed")
        if body is not None:
            changes.append("body replaced")
        if notebook_id is not None:
            changes.append(f"moved to {self._notebook_title(notebook_id) or 'root'}")
        return NoteUpdatedResponse(
            id=note_id,
            title=new_title,
            message=f"Note {note_id} updated successfully ({', '.join(changes)})",
        )

    async def delete_note(self, note_id: str) -> NoteDeletedResponse | JoplinError:
        """Delete a note by ID."""
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        try:
            resp = await self._api("GET", f"/api/items/root:/{note_id}.md:/content")
            parsed = _parse_joplin_item(resp.text)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Note {note_id} not found")
            raise
        if parsed["type"] != TYPE_NOTE:
            return JoplinError(error=f"Item {note_id} is not a note")
        guard = await self._note_guard(parsed)
        if guard:
            return guard

        await self._delete_item(note_id)
        return NoteDeletedResponse(
            id=note_id,
            title=parsed["title"],
            message=f"Note '{parsed['title']}' deleted successfully",
        )

    # -- Notebooks ----------------------------------------------------------

    async def ping(self) -> PingResponse:
        """Check connectivity and report what the index can see."""
        try:
            await self._get_session()
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return PingResponse(
                ok=False, server_url=self.url, message=f"Connection failed: {exc}"
            )
        details: dict[str, Any] = {}
        try:
            await self.index(force_refresh=True)
            details = {
                "indexed_items": len(self._index),
                "notes": sum(
                    1 for item in self._index.values() if item["type"] == TYPE_NOTE
                ),
                "notebooks": sum(
                    1 for item in self._index.values() if item["type"] == TYPE_FOLDER
                ),
            }
        except Exception as exc:  # noqa: BLE001
            details = {"index_error": str(exc)}
        return PingResponse(
            ok=True,
            server_url=self.url,
            message=f"Connected to {self.url}",
            details=details,
        )

    async def list_notebooks(self) -> list[NotebookSummary]:
        """All notebooks, scoped to the configured root when one is set."""
        notebooks = await self._items_of_type(TYPE_FOLDER)
        allowed = self._allowed_notebook_ids()
        return [
            NotebookSummary(id=nb["id"], title=nb["title"], parent_id=nb["parent_id"])
            for nb in notebooks
            if allowed is None or nb["id"] in allowed
        ]

    async def get_notebook(self, notebook_id: str) -> NotebookDetail | JoplinError:
        """One notebook with its notes and immediate sub-notebooks."""
        if not _ID_RE.match(notebook_id):
            return _invalid_id(notebook_id, "notebook ID")
        await self.index()
        if not self._in_scope(notebook_id):
            return JoplinError(
                error=f"Notebook {notebook_id} is outside the configured root notebook"
            )

        notebook = self._index.get(notebook_id)
        if not notebook or notebook["type"] != TYPE_FOLDER:
            return JoplinError(error=f"Notebook {notebook_id} not found")

        sub_notebooks = sorted(
            (
                NotebookSummary(
                    id=item["id"], title=item["title"], parent_id=item["parent_id"]
                )
                for item in self._index.values()
                if item["type"] == TYPE_FOLDER and item["parent_id"] == notebook_id
            ),
            key=lambda nb: nb.title.lower(),
        )
        notes = sorted(
            (
                item
                for item in self._index.values()
                if item["type"] == TYPE_NOTE and item["parent_id"] == notebook_id
            ),
            key=lambda n: n["updated_time"],
            reverse=True,
        )
        note_tags = self._note_tags_map()
        return NotebookDetail(
            id=notebook["id"],
            title=notebook["title"],
            parent_id=notebook["parent_id"],
            parent_title=(
                self._notebook_title(notebook["parent_id"])
                if notebook["parent_id"]
                else ""
            ),
            updated_time=notebook["updated_time"],
            note_count=len(notes),
            sub_notebooks=sub_notebooks,
            notes=[self._note_summary(n, note_tags) for n in notes],
        )

    async def create_notebook(
        self, title: str, parent_id: str = ""
    ) -> NotebookCreatedResponse | JoplinError:
        """Create a notebook, inheriting the parent's share_id."""
        if parent_id and not _ID_RE.match(parent_id):
            return _invalid_id(parent_id, "parent notebook ID")
        await self.index()
        if not parent_id and self.root_notebook_id:
            parent_id = self.root_notebook_id
        if parent_id and not self._in_scope(parent_id):
            return JoplinError(
                error=f"Notebook {parent_id} is outside the configured root notebook"
            )

        nb_id = uuid.uuid4().hex
        share_id = await self._get_share_id(parent_id) if parent_id else ""
        await self._put_item(
            nb_id,
            _folder_template(nb_id, title, parent_id, now_iso(), share_id=share_id),
            share_id=share_id,
        )
        return NotebookCreatedResponse(
            id=nb_id, message=f"Notebook '{title}' created successfully (ID: {nb_id})"
        )

    async def get_or_create_notebook(
        self, path: str
    ) -> NotebookPathResult | JoplinError:
        """Resolve a ``/``-separated notebook path, creating missing levels.

        Resolution starts at the configured root notebook when one is set,
        otherwise at the top level.
        """
        names = [part.strip() for part in path.split("/") if part.strip()]
        if not names:
            return JoplinError(error="`path` must contain at least one notebook name.")

        await self.index(force_refresh=True)
        parent_id = self.root_notebook_id
        created: list[str] = []
        for name in names:
            match = next(
                (
                    item
                    for item in self._index.values()
                    if item["type"] == TYPE_FOLDER
                    and item["parent_id"] == parent_id
                    and item["title"] == name
                ),
                None,
            )
            if match:
                parent_id = match["id"]
                continue
            if parent_id and not self._in_scope(parent_id):
                return JoplinError(
                    error=(
                        f"Cannot create '{name}': parent {parent_id} is outside"
                        " the configured root notebook"
                    )
                )
            nb_id = uuid.uuid4().hex
            share_id = await self._get_share_id(parent_id) if parent_id else ""
            await self._put_item(
                nb_id,
                _folder_template(nb_id, name, parent_id, now_iso(), share_id=share_id),
                share_id=share_id,
            )
            parent_id = nb_id
            created.append(name)

        suffix = f" — created: {', '.join(created)}" if created else " — all existed"
        return NotebookPathResult(
            path=path,
            notebook_id=parent_id,
            title=names[-1],
            created=created,
            message=f"Notebook path '{path}' ready (leaf ID: {parent_id}){suffix}",
        )

    def _is_descendant(self, candidate_id: str, ancestor_id: str) -> bool:
        """True when ``candidate_id`` sits at or below ``ancestor_id``."""
        seen: set[str] = set()
        current = candidate_id
        while current and current not in seen:
            if current == ancestor_id:
                return True
            seen.add(current)
            item = self._index.get(current)
            current = item["parent_id"] if item else ""
        return False

    async def update_notebook(
        self,
        notebook_id: str,
        title: str | None = None,
        parent_id: str | None = None,
    ) -> NotebookUpdatedResponse | JoplinError:
        """Rename a notebook or move it, refusing a circular move."""
        if not _ID_RE.match(notebook_id):
            return _invalid_id(notebook_id, "notebook ID")
        if title is None and parent_id is None:
            return JoplinError(error="Provide at least title or parent_id to update.")
        if parent_id and not _ID_RE.match(parent_id):
            return _invalid_id(parent_id, "parent notebook ID")

        await self.index()
        if not self._in_scope(notebook_id):
            return JoplinError(
                error=f"Notebook {notebook_id} is outside the configured root notebook"
            )
        notebook = self._index.get(notebook_id)
        if not notebook or notebook["type"] != TYPE_FOLDER:
            return JoplinError(error=f"Notebook {notebook_id} not found")

        if parent_id is not None:
            if parent_id == notebook_id:
                return JoplinError(error="Cannot move a notebook into itself.")
            if parent_id and not self._in_scope(parent_id):
                return JoplinError(
                    error=f"Notebook {parent_id} is outside the configured root notebook"
                )
            if parent_id and parent_id not in self._index:
                return JoplinError(error=f"Notebook {parent_id} not found")
            if parent_id and self._is_descendant(parent_id, notebook_id):
                return JoplinError(
                    error=(
                        f"Cannot move {notebook_id} into {parent_id}: the target"
                        " is inside the notebook being moved"
                    )
                )

        new_title = title if title is not None else notebook["title"]
        meta = dict(notebook["metadata"])
        if parent_id is not None:
            meta["parent_id"] = parent_id
            if parent_id:
                share_id = await self._get_share_id(parent_id)
                if share_id:
                    meta["share_id"] = share_id
                    meta["is_shared"] = "1"
        now = now_iso()
        meta["updated_time"] = now
        meta["user_updated_time"] = now
        await self._put_item(
            notebook_id,
            _serialize_item(new_title, "", meta),
            meta.get("share_id", "").strip(),
        )

        changes = []
        if title is not None:
            changes.append(f"renamed to '{new_title}'")
        if parent_id is not None:
            changes.append(f"moved to {self._notebook_title(parent_id) or 'root'}")
        return NotebookUpdatedResponse(
            id=notebook_id,
            message=f"Notebook {notebook_id} updated successfully ({', '.join(changes)})",
        )

    def _descendants(self, notebook_id: str) -> list[dict[str, Any]]:
        """Every note and sub-notebook below ``notebook_id``, deepest first."""
        found: list[tuple[int, dict[str, Any]]] = []
        frontier = [(0, notebook_id)]
        while frontier:
            depth, parent = frontier.pop()
            for item in self._index.values():
                if item["parent_id"] != parent:
                    continue
                if item["type"] in (TYPE_NOTE, TYPE_FOLDER):
                    found.append((depth + 1, item))
                    if item["type"] == TYPE_FOLDER:
                        frontier.append((depth + 1, item["id"]))
        # Deepest first so a parent is never deleted before its children.
        return [item for _, item in sorted(found, key=lambda pair: -pair[0])]

    async def delete_notebook(
        self, notebook_id: str, force: bool = False
    ) -> NotebookDeletedResponse | JoplinError:
        """Delete a notebook. Refuses a non-empty notebook unless ``force``."""
        if not _ID_RE.match(notebook_id):
            return _invalid_id(notebook_id, "notebook ID")
        if notebook_id == self.root_notebook_id:
            return JoplinError(error="Cannot delete the configured root notebook")

        await self.index()
        if not self._in_scope(notebook_id):
            return JoplinError(
                error=f"Notebook {notebook_id} is outside the configured root notebook"
            )
        notebook = self._index.get(notebook_id)
        if not notebook or notebook["type"] != TYPE_FOLDER:
            return JoplinError(error=f"Notebook {notebook_id} not found")

        children = self._descendants(notebook_id)
        if children and not force:
            notes = sum(1 for child in children if child["type"] == TYPE_NOTE)
            folders = len(children) - notes
            return JoplinError(
                error=(
                    f"Notebook '{notebook['title']}' is not empty "
                    f"({notes} notes, {folders} sub-notebooks). "
                    "Set force=True to delete."
                ),
                hint="force=True deletes every descendant; list them with get_notebook first.",
            )

        deleted: list[str] = []
        for child in children:
            try:
                await self._delete_item(child["id"])
                deleted.append(child["title"])
            except Exception as exc:  # noqa: BLE001 - report and keep going
                log.warning("Failed to delete %s: %s", child["id"], exc)
        await self._delete_item(notebook_id)

        message = f"Notebook '{notebook['title']}' deleted successfully"
        if deleted:
            message += f" (also deleted {len(deleted)} item(s))"
        return NotebookDeletedResponse(
            id=notebook_id, message=message, deleted_children=deleted
        )

    # -- Tags ---------------------------------------------------------------

    async def list_tags(self) -> list[TagSummary]:
        """All tags."""
        tags = await self._items_of_type(TYPE_TAG)
        return [TagSummary(id=tag["id"], title=tag["title"]) for tag in tags]

    async def create_tag(self, title: str) -> TagCreatedResponse:
        """Create a tag."""
        tag_id = uuid.uuid4().hex
        await self._put_item(tag_id, _tag_template(tag_id, title, now_iso()))
        return TagCreatedResponse(
            id=tag_id, message=f"Tag '{title}' created successfully (ID: {tag_id})"
        )

    async def delete_tag(self, tag_id: str) -> TagDeletedResponse | JoplinError:
        """Delete a tag and every note-tag link that points at it."""
        if not _ID_RE.match(tag_id):
            return _invalid_id(tag_id, "tag ID")
        await self.index()
        tag = self._index.get(tag_id)
        if not tag or tag["type"] != TYPE_TAG:
            return JoplinError(error=f"Tag {tag_id} not found")

        links = self._note_tag_links(tag_id=tag_id)
        for link in links:
            try:
                await self._delete_item(link["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to delete note-tag %s: %s", link["id"], exc)
        await self._delete_item(tag_id)
        return TagDeletedResponse(
            id=tag_id,
            message=(
                f"Tag '{tag['title']}' deleted successfully "
                f"(removed from {len(links)} note(s))"
            ),
            removed_from_notes=len(links),
        )

    async def get_note_tags(self, note_id: str) -> list[TagSummary] | JoplinError:
        """Tags assigned to a note."""
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        await self.index()
        note = self._index.get(note_id)
        if not note or note["type"] != TYPE_NOTE:
            return JoplinError(error=f"Note {note_id} not found")
        guard = await self._note_guard(note)
        if guard:
            return guard

        titles: list[TagSummary] = []
        for link in self._note_tag_links(note_id=note_id):
            tag_id = link["metadata"].get("tag_id", "")
            tag = self._index.get(tag_id)
            titles.append(
                TagSummary(id=tag_id, title=tag["title"] if tag else tag_id[:12])
            )
        return titles

    async def add_tag_to_note(
        self, tag_id: str, note_id: str
    ) -> TagAddedResponse | JoplinError:
        """Add a tag to a note, refusing a duplicate."""
        for value, label in ((tag_id, "tag ID"), (note_id, "note ID")):
            if not _ID_RE.match(value):
                return _invalid_id(value, label)

        await self.index()
        tag = self._index.get(tag_id)
        if not tag or tag["type"] != TYPE_TAG:
            return JoplinError(error=f"Tag {tag_id} not found")
        note = self._index.get(note_id)
        if not note or note["type"] != TYPE_NOTE:
            return JoplinError(error=f"Note {note_id} not found")
        guard = await self._note_guard(note)
        if guard:
            return guard

        if self._note_tag_links(note_id=note_id, tag_id=tag_id):
            return JoplinError(
                error=f"Tag '{tag['title']}' already on note '{note['title']}'"
            )

        nt_id = uuid.uuid4().hex
        share_id = (
            await self._get_share_id(note["parent_id"]) if note["parent_id"] else ""
        )
        await self._put_item(
            nt_id,
            _note_tag_template(nt_id, note_id, tag_id, tag["title"], now_iso()),
            share_id=share_id,
        )
        return TagAddedResponse(
            id=nt_id,
            message=f"Tag '{tag['title']}' added to note '{note['title']}'",
        )

    async def remove_tag_from_note(
        self, tag_id: str, note_id: str
    ) -> TagRemovedResponse | JoplinError:
        """Remove a tag from a note."""
        for value, label in ((tag_id, "tag ID"), (note_id, "note ID")):
            if not _ID_RE.match(value):
                return _invalid_id(value, label)

        await self.index()
        note = self._index.get(note_id)
        if not note or note["type"] != TYPE_NOTE:
            return JoplinError(error=f"Note {note_id} not found")
        guard = await self._note_guard(note)
        if guard:
            return guard

        links = self._note_tag_links(note_id=note_id, tag_id=tag_id)
        if not links:
            return JoplinError(error="Tag is not assigned to this note")
        for link in links:
            await self._delete_item(link["id"])

        tag = self._index.get(tag_id)
        return TagRemovedResponse(
            id=links[0]["id"],
            message=(
                f"Tag '{tag['title'] if tag else tag_id}' removed"
                f" from note '{note['title']}'"
            ),
        )

    # -- Resources ----------------------------------------------------------

    async def get_note_resources(self, note_id: str) -> NoteResources | JoplinError:
        """Resources referenced by a note."""
        if not _ID_RE.match(note_id):
            return _invalid_id(note_id, "note ID")
        await self.index()
        note = self._index.get(note_id)
        if not note or note["type"] != TYPE_NOTE:
            return JoplinError(error=f"Note {note_id} not found")
        guard = await self._note_guard(note)
        if guard:
            return guard

        await self._ensure_resource_index()
        return NoteResources(
            id=note["id"],
            title=note["title"],
            resources=[
                self._resource_ref(ref) for ref in _find_resource_refs(note["body"])
            ],
        )

    async def get_resource_info(self, resource_id: str) -> ResourceInfo | JoplinError:
        """Metadata for one resource, fetched directly when it is not indexed."""
        if not _ID_RE.match(resource_id):
            return _invalid_id(resource_id, "resource ID")
        await self._ensure_resource_index()
        res = self._resource_index.get(resource_id)
        if not res:
            try:
                resp = await self._api(
                    "GET", f"/api/items/root:/{resource_id}.md:/content"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return JoplinError(error=f"Resource {resource_id} not found")
                raise
            res = _parse_resource_metadata(resp.text)
        return ResourceInfo(
            id=res.get("id", resource_id),
            title=res.get("title", ""),
            mime=res.get("mime", ""),
            size=_int_or_zero(res.get("size")),
            file_extension=res.get("file_extension", ""),
            created_time=res.get("created_time", ""),
            updated_time=res.get("updated_time", ""),
        )

    async def download_resource(
        self, resource_id: str
    ) -> ResourceContent | JoplinError:
        """Download a resource as base64. Refuses anything over 50 MB."""
        if not _ID_RE.match(resource_id):
            return _invalid_id(resource_id, "resource ID")
        await self._ensure_resource_index()
        res = self._resource_index.get(resource_id)
        if res and _int_or_zero(res.get("size")) > MAX_RESOURCE_SIZE:
            size = _int_or_zero(res.get("size"))
            return JoplinError(
                error=f"Resource too large ({size / 1024 / 1024:.1f} MB). Max 50 MB.",
                hint="Use get_note_resources to find a smaller attachment.",
            )
        try:
            content = await self._resource_content(resource_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return JoplinError(error=f"Resource {resource_id} not found")
            raise
        if content.error:
            # ``get_note_full`` reports a bad attachment inline, but asking for
            # one resource by ID and not getting it is a failure, not a result.
            return JoplinError(error=content.error)
        return content
