# Donetick MCP Server

An [MCP](https://modelcontextprotocol.io/) server for managing household chores via the [Donetick](https://donetick.com/) API, built with [FastMCP](https://gofastmcp.com/).

Targets Donetick **v0.1.79**.

## Tools

### Reading chores

| Tool | Description |
|------|-------------|
| `list_chores` | List active chores with summary info |
| `search_chores` | Search chores by name, description, or label |
| `get_chore` | Get full details of a single chore |
| `get_chore_history` | Completion history for one chore |
| `get_history` | Completion history across the circle |
| `list_archived_chores` | List archived chores |

### Changing chores

| Tool | Description |
|------|-------------|
| `create_chore` | Create a chore, with assignees, recurrence, labels, and reminders |
| `update_chore` | Update a chore from a full replacement payload |
| `update_due_date` | Move a chore's due date without rewriting the record |
| `complete_chore` | Mark a chore done (recurring chores auto-reschedule) |
| `skip_chore` | Skip one occurrence without completing the chore |
| `archive_chore` | Archive a chore, keeping its history |
| `unarchive_chore` | Restore an archived chore |
| `delete_chore` | Permanently delete a chore (requires `confirm=True`) |

### Assignment and approval

| Tool | Description |
|------|-------------|
| `list_users` | Circle members, for resolving names to user IDs |
| `approve_chore` | Approve a completion awaiting approval |
| `reject_chore` | Reject a completion awaiting approval |

### Timers

| Tool | Description |
|------|-------------|
| `start_chore_timer` | Start timing work on a chore |
| `pause_chore_timer` | Pause a running chore timer |

### Subtasks

| Tool | Description |
|------|-------------|
| `list_subtasks` | Checklist items on a chore |
| `create_subtask` | Add a checklist item |
| `update_subtask_completion` | Tick or untick a checklist item |
| `delete_subtask` | Remove a checklist item |

### Labels

| Tool | Description |
|------|-------------|
| `list_labels` | Circle labels |
| `create_label` | Create a label |
| `update_label` | Rename or recolour a label |
| `delete_label` | Delete a label |

## Resources

| URI | Contents |
|-----|----------|
| `donetick://profile` | The authenticated user's profile |
| `donetick://chores` | Every active chore, as a summary list |
| `donetick://chore/{chore_id}` | Full detail of one chore |
| `donetick://users` | Circle members, for resolving user IDs |
| `donetick://labels` | Circle labels available for chore assignment |

All resources return JSON text.

## Setup

### Prerequisites

- Python 3.12+
- A [Donetick](https://donetick.com/) account

### Installation

```bash
uv sync
```

### Configuration

Only the credentials are required:

```bash
export DONETICK_URL="https://donetick.com/"   # or your self-hosted instance
export DONETICK_USERNAME="your-username"
export DONETICK_PASSWORD="your-password"
```

Everything else has a working default:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DONETICK_URL` | `https://donetick.com/` | API base URL |
| `DONETICK_USERNAME` | *(required)* | Account username |
| `DONETICK_PASSWORD` | *(required)* | Account password |
| `DONETICK_TIMEZONE` | `UTC` | Timezone for due dates and reminder times. Any [IANA name](https://www.iana.org/time-zones) (e.g. `America/New_York`, `Europe/London`) |
| `DONETICK_TIMEOUT` | `10` | Per-request timeout, in seconds |
| `DONETICK_RATE_LIMIT_PER_SECOND` | `10` | Sustained request rate |
| `DONETICK_RATE_LIMIT_BURST` | `10` | Burst allowance |
| `DONETICK_MAX_RETRIES` | `3` | Retries for 429s, 5xx, and transport errors |
| `DONETICK_VERIFY_TLS` | `true` | Set `false` only for a self-signed instance |
| `DONETICK_LOG_LEVEL` | `WARNING` | Python log level |

### Multi-factor accounts

Donetick's MFA flow needs a `sessionToken` round trip that this server does not
perform. If the account has MFA enabled, login fails with an explicit error
telling you to use a dedicated API/automation user.

## Usage

### Run directly

```bash
donetick-mcp
# or
python -m donetick_mcp
```

### Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "donetick": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/packages/donetick-mcp", "donetick-mcp"],
      "env": {
        "DONETICK_URL": "https://donetick.com/",
        "DONETICK_USERNAME": "your-username",
        "DONETICK_PASSWORD": "your-password",
        "DONETICK_TIMEZONE": "America/New_York"
      }
    }
  }
}
```

## Notes on the Donetick API

The client is defensive here because the upstream API has sharp edges. Each of
these was verified against a live v0.1.79 instance:

- **Due dates are `nextDueDate`.** The old `dueDate` field is silently ignored.
- **`labelsV2` and `subTasks` must always be present on update.** Donetick's
  edit handler dereferences both without a nil check, so omitting either
  crashes the server and drops the connection with no HTTP response.
- **Several endpoints report failure as `200` with an `error` key** rather than
  an error status. Starting an already-running timer does this. The client
  turns those into exceptions.
- **Never send `updatedAt` on an edit.** Donetick uses it as an optimistic
  lock, and its check is unforgiving: a stale value 403s as "modified by
  another user" -- but so does the value you just read from a fresh `GET`,
  because the server stamps `UpdatedAt` slightly ahead of what a client can
  observe. Omitting it skips the conflict check.
- **Trailing slashes are inconsistent.** `/api/v1/chores/` and `/api/v1/users/`
  need one; `/api/v1/labels` and `/api/v1/circles/members` must not have one.
- **Envelopes vary.** Most responses wrap in `{"res": ...}`, but
  `GET /api/v1/labels` returns a bare array.
- **`frequency` must be at least 1.** `0` is accepted but breaks
  `day_of_the_month` rescheduling.

## Development

### Tests

Unit tests run entirely in-process against an `httpx.MockTransport`:

```bash
uv run pytest packages/donetick-mcp/tests/
```

Integration tests are marked `@pytest.mark.integration` and are skipped unless
`DONETICK_INTEGRATION=1` is set. They download the official release binary, run
it locally against a throwaway SQLite database on a free port, and tear it down
afterwards:

```bash
DONETICK_INTEGRATION=1 uv run pytest packages/donetick-mcp/tests/
```

The binary is cached between runs, and the version is pinned by
`DONETICK_VERSION` (override it to test a different release).

### Interactive testing

Use the FastMCP inspector:

```bash
fastmcp dev src/donetick_mcp/server.py:mcp
```