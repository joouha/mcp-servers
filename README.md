# MCP Servers

A collection of [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers for integrating AI assistants with various self-hosted services. Each server is built with [FastMCP](https://gofastmcp.com/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

## Servers

| Package | Description | Service |
|---------|-------------|---------|
| [caldav-mcp](packages/caldav-mcp/) | Manage calendar events via CalDAV | [Nextcloud](https://nextcloud.com/), [Radicale](https://radicale.org/), [Baikal](https://sabre.io/baikal/), etc. |
| [donetick-mcp](packages/donetick-mcp/) | Manage household chores | [Donetick](https://donetick.com/) |
| [tandoor-mcp](packages/tandoor-mcp/) | Manage meal plans and recipes | [Tandoor Recipes](https://tandoor.dev/) |

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Getting Started

Install all packages in the workspace:

```bash
uv sync
```

Each server can then be run directly:

```bash
caldav-mcp
donetick-mcp
tandoor-mcp
```

See the individual package READMEs for configuration details and Claude Desktop integration instructions.

## Project Structure

```
.
├── packages/
│   ├── caldav-mcp/          # CalDAV calendar server
│   ├── donetick-mcp/        # Donetick chores server
│   └── tandoor-mcp/         # Tandoor meal planning server
├── pyproject.toml            # Workspace root
└── uv.lock
```

## Development

Each server exposes a FastMCP app that can be tested interactively with the FastMCP inspector:

```bash
fastmcp dev packages/caldav-mcp/src/caldav_mcp/__init__.py:mcp
fastmcp dev packages/donetick-mcp/src/donetick_mcp/__init__.py:mcp
fastmcp dev packages/tandoor-mcp/src/tandoor_mcp/__init__.py:mcp
```

## License

This project is for personal use.
