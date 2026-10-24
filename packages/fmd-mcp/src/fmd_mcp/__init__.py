"""FMD MCP Server - Access your Find My Device server via MCP.

The implementation is split across focused modules:

* :mod:`fmd_mcp.config` - environment-driven configuration
* :mod:`fmd_mcp.models` - request/response schemas
* :mod:`fmd_mcp.client` - async FMD API client wrapper
* :mod:`fmd_mcp.server` - FastMCP tools and resources

Everything public is re-exported here for convenience.
"""

from __future__ import annotations

from .client import (
    FmdMcpAuthError,
    FmdMcpClient,
    FmdMcpError,
    FmdMcpNotFoundError,
)
from .config import Config
from .models import (
    DeviceInfo,
    ErrorResponse,
    Location,
    LocationHistoryResponse,
    MessageResponse,
    PictureMetadata,
    RequestLocationResponse,
)
from .server import mcp

__all__ = [
    "Config",
    "DeviceInfo",
    "ErrorResponse",
    "FmdMcpAuthError",
    "FmdMcpClient",
    "FmdMcpError",
    "FmdMcpNotFoundError",
    "Location",
    "LocationHistoryResponse",
    "MessageResponse",
    "PictureMetadata",
    "RequestLocationResponse",
    "main",
    "mcp",
]


def main() -> None:
    """Run the FMD MCP server over stdio."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()