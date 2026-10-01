# joplin-mcp

An [MCP](https://modelcontextprotocol.io/) server for managing notes, notebooks, tags, and attachments on a [Joplin Server](https://joplinapp.org/), built with [FastMCP](https://gofastmcp.com/).

The point of interest is the **partial editors** — `append_to_note`, `replace_in_note`, and `replace_section`. They resolve the edit server-side, so a model sends only the fragment that changes and never has to reproduce, or even read, the rest of a note. Untouched bytes are preserved verbatim, including the `:/<resource-id>` links the read tools render as filenames.

Every tool returns a structured JSON object (not prose), and every write reports exactly what it did.

## Tools

### Connectivity

| Tool | Description |
|------|-------------|
| `ping_joplin` | Check connectivity and report what the index can see |

### Notebooks

| Tool | Description |
|------|-------------|
| `list_notebooks` | List all notebooks with IDs and parents |
| `get_notebook` | One notebook with its notes and sub-notebooks |
| `create_notebook` | Create a notebook, optionally nested |
| `get_or_create_notebook` | Resolve a `A/B/C` path, creating only the missing levels |
| `update_notebook` | Rename or move a notebook |
| `delete_notebook` | Delete a notebook; refuses a non-empty one unless `force=True` |

### Notes — reading

| Tool | Description |
|------|-------------|
| `list_notes` | List notes, filtered by notebook, tag, and/or to-do state |
| `get_all_notes` | Every note, with pagination and sorting |
| `search_notes` | Text search; every whitespace-separated term must match |
| `get_note` | One note in full; `raw=True` keeps resource links verbatim |
| `get_notes_batch` | Read up to 50 notes in parallel |
| `get_note_full` | A note with every resource inlined as base64 |
| `export_note` | A note as markdown with plain filenames plus base64 resources |
| `get_note_outline` | Headings with line numbers and span sizes — no body text |

### Notes — writing

| Tool | Description |
|------|-------------|
| `create_note` | Create a note, optionally as a to-do with a due date |
| `update_note` | Replace a note's title, body, and/or notebook in one write |
| `append_to_note` | Add text into or beside a section |
| `replace_in_note` | Replace an exact string inside the body |
| `replace_section` | Replace everything under a heading |
| `set_todo` | Set or clear to-do state, completion, and due date |
| `delete_note` | Delete a note |

### Tags

| Tool | Description |
|------|-------------|
| `list_tags` | List all tags |
| `create_tag` | Create a tag |
| `delete_tag` | Delete a tag and remove it from all notes |
| `get_note_tags` | Tags assigned to a note |
| `add_tag_to_note` | Add a tag to a note |
| `remove_tag_from_note` | Remove a tag from a note |

### Resources

| Tool | Description |
|------|-------------|
| `get_note_resources` | Resources referenced by a note |
| `get_resource_info` | Metadata for one resource |
| `download_resource` | A resource as base64; refuses anything over 50 MB |

> **`edit_note` was removed.** It replaced the first match of an anchor without checking for others. `replace_in_note` supersedes it: it requires the match to be unique unless you pass `replace_all=True`, and it refuses rather than guessing. Use `replace_section` when you want to address content by heading instead of by literal text.

## Partial edits

The three partial editors share one shape: read the note, splice the change in memory, write the whole item back with its metadata block intact, and report what changed. Nothing but the intended span moves.

### `append_to_note`

```jsonc
// Add a row to a table, without the blank line a default separator would add
append_to_note(note_id, "| host-04 | ok |", section="Hosts", separator="\n")
```

`position` decides where the text lands, and the distinction matters:

| Position | Goes | Requires `section` | Honours `separator` |
|----------|------|--------------------|---------------------|
| `end` (default) | After the section's own content | No | Yes |
| `start` | Before the section's content | No | Yes |
| `before` | Above the heading, as a sibling | **Yes** | No |
| `after` | Below the section's whole span — subsections included | **Yes** | No |

`before` and `after` are strictly additive: they insert a block beside a section and never rewrite an existing byte. That makes `before` the way to put a new entry at the top of a newest-first log whose preamble you do not want to disturb.

### `replace_in_note`

```jsonc
replace_in_note(note_id, "Deployed.", "Rolled back.")
```

Refuses, with a message telling you what to do next, when:

- `old_text` is not in the body (hint: copy the anchor from `get_note(raw=True)`),
- it matches more than once and `replace_all` is not set — the error names the line numbers,
- it only differs by surrounding whitespace.

Pass `new_text: ""` to delete the anchor.

### `replace_section`

```jsonc
replace_section(note_id, "Hosts", "- host-04 — deployed")
```

Addresses content by heading instead of by literal text. The heading line is kept; everything under it, up to the next heading of the same or higher level, is replaced. Pass `""` to empty a section.

### Sections are resolved, never guessed

`section` accepts bare heading text (`"Hosts"`) or the full line (`"## Hosts"`; the level must then match). Matching goes exact → case-insensitive → unique case-insensitive substring. If a reference is **missing or ambiguous the edit is refused** and the offending headings are listed — a wrong guess here silently rewrites the wrong part of a long note. Headings inside fenced code blocks are ignored. Call `get_note_outline` when you need to know what a note actually contains; it returns line numbers without the body.

Refusals say what went wrong and what to do about it, rather than returning a generic failure:

```jsonc
{
  "error": "Section 'Log' is ambiguous (2 matches): line 1: ## Log; line 5: ## Log. "
           "Give the full heading text with its '#' prefix, or use replace_in_note.",
  "hint": "Call get_note_outline to list the note's headings."
}
```

### Safeguards on every write

| Guard | Behaviour |
|-------|-----------|
| `dry_run=True` | Reports the change, its deltas, and its context lines without writing |
| `if_absent="..."` | Skips the write when the marker is already present, making a re-run a no-op |
| Unique anchors | `replace_in_note` refuses a multi-match rather than picking one |
| Scoped sections | `before`/`after` without a `section` are refused, not silently applied to the whole note |
| Exact-ID validation | Every ID argument is checked against Joplin's 32-hex format before it reaches the URL |

### What a write reports

Every edit returns the same `EditReport`: character, line, and heading counts before and after with their deltas, the 1-based line the change landed on, and numbered context lines around it. Enough to confirm the edit without re-reading the note.

```jsonc
// append_to_note(note_id, "| host-04 | ok |", section="Hosts", separator="\n")
{
  "action": "append",
  "changed": true,
  "dry_run": false,
  "detail": "Inserted 16 chars at the end of section '## Hosts' (line 5)",
  "chars_before": 127,  "chars_after": 144,
  "lines_before": 15,   "lines_after": 16,
  "headings_before": 3, "headings_after": 3,
  "line": 12,
  "context_lines": [
    " 9 | | host-01 | ok |",
    "10 | | host-02 | ok |",
    "11 | | host-03 | ok |",
    "12 | | host-04 | ok |",
    "13 | ",
    "14 | ## Notes",
    "15 | ",
    "16 | Tail."
  ],
  "message": "Inserted 16 chars at the end of section '## Hosts' (line 5). Chars 127 -> 144 (+17), lines 15 -> 16 (+1), headings 3 -> 3 (+0); change at line 12."
}
```

## Configuration

| Environment Variable | Description | Required |
|---------------------|-------------|----------|
| `JOPLIN_SERVER_URL` | URL of the Joplin Server (e.g. `https://joplin.example.com`) | Yes |
| `JOPLIN_EMAIL` | Email address for authentication | Yes |
| `JOPLIN_PASSWORD` | Password for authentication | Yes |
| `JOPLIN_NOTEBOOK_ID` | Restrict operations to this notebook and its children | No |
| `JOPLIN_INDEX_CACHE_FILE` | Where to persist the item index; empty disables it | No |

With `JOPLIN_NOTEBOOK_ID` set, notebooks outside that subtree are hidden and any write targeting them is refused — including a delete of the configured root itself.

## Usage

```bash
export JOPLIN_SERVER_URL=https://joplin.example.com
export JOPLIN_EMAIL=user@example.com
export JOPLIN_PASSWORD=secret
# Optional: restrict to a single notebook tree
# export JOPLIN_NOTEBOOK_ID=abcdef01234567890abcdef012345678
joplin-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "joplin": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/packages/joplin-mcp", "joplin-mcp"],
      "env": {
        "JOPLIN_SERVER_URL": "https://joplin.example.com",
        "JOPLIN_EMAIL": "user@example.com",
        "JOPLIN_PASSWORD": "secret"
      }
    }
  }
}
```

### Development

```bash
uv sync
fastmcp dev packages/joplin-mcp/src/joplin_mcp/__init__.py:mcp
```

## Performance

Listing, filtering, and searching run against a local index of every item rather than re-downloading the account on every call. The index syncs incrementally — only items whose server-side `updated_time` moved are re-fetched — behind a short TTL, and is cached on disk per account so a restart starts warm. A stale index is served immediately while the refresh runs in the background, so reads never block on a sync; the create and delete paths force a sync because they must act on current server state.

## Tests

```bash
uv run --directory packages/joplin-mcp pytest tests/test_helpers.py tests/test_edits.py -q
```

- `tests/test_helpers.py` — the pure markdown, splice, and to-do helpers. No network, no server.
- `tests/test_edits.py` — the tools end to end against an in-memory fake Joplin Server, including every refusal.

`tests/test_readonly.py` is a live smoke test that needs real credentials:

```bash
JOPLIN_SERVER_URL=... JOPLIN_EMAIL=... JOPLIN_PASSWORD=... \
  uv run --directory packages/joplin-mcp python tests/test_readonly.py
```
