# joplin-mcp

An MCP server for managing notes, notebooks, and tags on a [Joplin Server](https://joplinapp.org/).

## Tools

| Tool | Description |
|------|-------------|
| `list_notebooks` | List all notebooks |
| `get_notebook` | Get details of a single notebook |
| `create_notebook` | Create a new notebook (folder) |
| `update_notebook` | Update a notebook's title or parent |
| `delete_notebook` | Delete a notebook (optionally with contents) |
| `list_notes` | List notes, optionally filtered by notebook |
| `search_notes` | Search notes by text in title or body |
| `get_note` | Get full details of a note including its body |
| `create_note` | Create a new note |
| `update_note` | Replace a note's title, body, or notebook |
| `edit_note` | Surgically edit a note by replacing matching text |
| `delete_note` | Delete a note |
| `list_tags` | List all tags |
| `create_tag` | Create a new tag |
| `delete_tag` | Delete a tag and remove it from all notes |
| `get_note_tags` | List tags assigned to a note |
| `add_tag_to_note` | Add a tag to a note |
| `remove_tag_from_note` | Remove a tag from a note |

### Editing notes

The `edit_note` tool provides a search-and-replace style interface for making targeted edits to a note's body without replacing the entire content. It reads the note first, finds `old_string` in the body, and replaces it with `new_string`. Set `replace_all` to `true` to replace every occurrence rather than just the first.

## Configuration

| Environment Variable | Description | Required |
|---------------------|-------------|----------|
| `JOPLIN_SERVER_URL` | URL of the Joplin Server (e.g. `https://joplin.example.com`) | Yes |
| `JOPLIN_EMAIL` | Email address for authentication | Yes |
| `JOPLIN_PASSWORD` | Password for authentication | Yes |
| `JOPLIN_NOTEBOOK_ID` | Restrict operations to this notebook and its children | No |

## Usage

```bash
export JOPLIN_SERVER_URL=https://joplin.example.com
export JOPLIN_EMAIL=user@example.com
export JOPLIN_PASSWORD=secret
# Optional: restrict to a single notebook tree
# export JOPLIN_NOTEBOOK_ID=abcdef01234567890abcdef012345678
joplin-mcp
```

### With FastMCP dev mode

```bash
fastmcp dev packages/joplin-mcp/src/joplin_mcp/__init__.py:mcp
```
