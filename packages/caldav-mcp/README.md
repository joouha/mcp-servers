# CalDAV MCP Server

An [MCP](https://modelcontextprotocol.io/) server for managing calendar events via a [CalDAV](https://en.wikipedia.org/wiki/CalDAV) server, built with [FastMCP](https://gofastmcp.com/).

## Tools

| Tool | Description |
|------|-------------|
| `list_calendars` | List all calendars available on the server |
| `list_events` | List all events on the configured calendar |
| `search_events` | Search events by date range and/or text query |
| `get_event` | Get full details of a single event by UID |
| `create_event` | Create a new calendar event |
| `update_event` | Update an existing event (partial updates supported) |
| `delete_event` | Delete a calendar event |

## Setup

### Prerequisites

- Python 3.14+
- A CalDAV server (e.g. [Nextcloud](https://nextcloud.com/), [Radicale](https://radicale.org/), [Baikal](https://sabre.io/baikal/))

### Installation

```bash
uv sync
```

### Configuration

Set these environment variables:

```bash
export CALDAV_URL="https://your-caldav-server.example.com"   # CalDAV server URL
export CALDAV_USERNAME="your-username"
export CALDAV_PASSWORD="your-password"
export CALDAV_CALENDAR_URL=""                                 # optional, specific calendar URL
export CALDAV_TIMEZONE="UTC"                                  # optional, default UTC
```

If `CALDAV_CALENDAR_URL` is not set, use the `list_calendars` tool to discover available calendars and their URLs.

## Usage

### Run directly

```bash
caldav-mcp
```

### Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "caldav": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/packages/caldav-mcp", "caldav-mcp"],
      "env": {
        "CALDAV_URL": "https://your-caldav-server.example.com",
        "CALDAV_USERNAME": "your-username",
        "CALDAV_PASSWORD": "your-password",
        "CALDAV_CALENDAR_URL": "https://your-caldav-server.example.com/calendars/user/default/",
        "CALDAV_TIMEZONE": "America/New_York"
      }
    }
  }
}
```

### Development

Use the FastMCP inspector for interactive testing:

```bash
fastmcp dev src/caldav_mcp/__init__.py:mcp
```
