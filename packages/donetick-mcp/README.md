# Donetick MCP Server

An [MCP](https://modelcontextprotocol.io/) server for managing household chores via the [Donetick](https://donetick.com/) API, built with [FastMCP](https://gofastmcp.com/).

## Tools

| Tool | Description |
|------|-------------|
| `list_chores` | List all chores with summary info |
| `search_chores` | Search chores by name or description |
| `get_chore` | Get full details of a single chore |
| `create_chore` | Create a new chore |
| `update_chore` | Update an existing chore (partial updates supported) |
| `complete_chore` | Mark a chore as done (recurring chores auto-reschedule) |
| `delete_chore` | Archive a chore |

## Setup

### Prerequisites

- Python 3.14+
- A [Donetick](https://donetick.com/) account

### Installation

```bash
uv sync
```

### Configuration

Set these environment variables:

```bash
export DONETICK_URL="https://donetick.com/"   # or your self-hosted instance
export DONETICK_USERNAME="your-username"
export DONETICK_PASSWORD="your-password"
export DONETICK_TIMEOUT="10"                   # optional, default 10s
```

## Usage

### Run directly

```bash
donetick-mcp
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
        "DONETICK_PASSWORD": "your-password"
      }
    }
  }
}
```

### Development

Use the FastMCP inspector for interactive testing:

```bash
fastmcp dev src/donetick_mcp/__init__.py:mcp
```
