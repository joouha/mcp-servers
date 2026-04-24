# MCP Servers

A collection of [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers for integrating AI assistants with various self-hosted services. Each server is built with [FastMCP](https://gofastmcp.com/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

## Servers

| Package | Description | Service |
|---------|-------------|---------|
| [browser-mcp](packages/browser-mcp/) | Browser automation (open, click, type, scroll) | [Camoufox](https://camoufox.com/) |
| [caldav-mcp](packages/caldav-mcp/) | Manage calendar events via CalDAV | [Nextcloud](https://nextcloud.com/), [Radicale](https://radicale.org/), [Baikal](https://sabre.io/baikal/), etc. |
| [donetick-mcp](packages/donetick-mcp/) | Manage household chores | [Donetick](https://donetick.com/) |
| [joplin-mcp](packages/joplin-mcp/) | Manage notes, notebooks, and tags | [Joplin Server](https://joplinapp.org/) |
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
browser-mcp
caldav-mcp
donetick-mcp
joplin-mcp
tandoor-mcp
```

See the individual package READMEs for configuration details and Claude Desktop integration instructions.

## Project Structure

```
.
├── packages/
│   ├── browser-mcp/         # Camoufox browser automation server
│   ├── caldav-mcp/          # CalDAV calendar server
│   ├── donetick-mcp/        # Donetick chores server
│   ├── joplin-mcp/          # Joplin notes server
│   └── tandoor-mcp/         # Tandoor meal planning server
├── pyproject.toml            # Workspace root
└── uv.lock
```

## Development

Each server exposes a FastMCP app that can be tested interactively with the FastMCP inspector:

```bash
fastmcp dev packages/browser-mcp/src/browser_mcp/__init__.py:mcp
fastmcp dev packages/caldav-mcp/src/caldav_mcp/__init__.py:mcp
fastmcp dev packages/donetick-mcp/src/donetick_mcp/__init__.py:mcp
fastmcp dev packages/joplin-mcp/src/joplin_mcp/__init__.py:mcp
fastmcp dev packages/tandoor-mcp/src/tandoor_mcp/__init__.py:mcp
```

## License

This project is for personal use.
