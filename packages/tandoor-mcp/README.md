# Tandoor MCP Server

An [MCP](https://modelcontextprotocol.io/) server for managing meal plans via [Tandoor Recipes](https://tandoor.dev/), built with [FastMCP](https://gofastmcp.com/).

## Tools

| Tool | Description |
|------|-------------|
| `list_meal_types` | List all meal types (e.g. breakfast, lunch, dinner) |
| `search_recipes` | Search recipes by name |
| `list_meal_plans` | List meal plan entries within a date range |
| `get_meal_plan` | Get full details of a single meal plan entry |
| `create_meal_plan` | Create a new meal plan entry |
| `update_meal_plan` | Update an existing meal plan entry (partial updates supported) |
| `delete_meal_plan` | Delete a meal plan entry |

## Setup

### Prerequisites

- Python 3.12+
- A [Tandoor Recipes](https://tandoor.dev/) instance with API access

### Installation

```bash
uv sync
```

### Configuration

Set these environment variables:

```bash
export TANDOOR_URL="https://tandoor.example.com"   # Tandoor server URL
export TANDOOR_API_KEY="your-api-key"               # Tandoor API bearer token
export TANDOOR_TIMEOUT="20"                         # optional, request timeout in seconds
export TANDOOR_TIMEZONE="UTC"                       # optional, default UTC
```

Generate an API key in Tandoor under **Settings → API Browser / Tokens**.

## Usage

### Run directly

```bash
tandoor-mcp
```

### Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tandoor": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/packages/tandoor-mcp", "tandoor-mcp"],
      "env": {
        "TANDOOR_URL": "https://tandoor.example.com",
        "TANDOOR_API_KEY": "your-api-key"
      }
    }
  }
}
```

### Development

Use the FastMCP inspector for interactive testing:

```bash
fastmcp dev src/tandoor_mcp/__init__.py:mcp
```
