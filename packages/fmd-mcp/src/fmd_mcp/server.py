"""FastMCP tools and resources exposing the FMD (Find My Device) API.

Every tool is async so a slow FMD instance cannot stall the event loop,
and every tool returns either a structured model or an ``ErrorResponse``
rather than raising, which keeps failures legible for the calling agent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Context, FastMCP

from .client import FmdMcpClient, FmdMcpAuthError, FmdMcpError, FmdMcpNotFoundError
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

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Build a single authenticated client for the server's lifetime."""
    config = Config.from_env()
    logging.getLogger(__name__).setLevel(config.log_level)
    client = FmdMcpClient(config)
    try:
        # Authenticate eagerly so a bad password fails at startup rather than
        # on the first tool call.
        await client.ensure_auth()
        yield {"client": client, "config": config}
    finally:
        await client.aclose()


mcp = FastMCP(
    "FMD (Find My Device)",
    instructions=(
        "Access your FMD (Find My Device) server to view location history, "
        "request current location, and send commands to your device. "
        "Configure with FMD_URL, FMD_USERNAME, FMD_PASSWORD environment variables. "
        "Optionally set FMD_DEVICE_ID to target a specific device."
    ),
    lifespan=lifespan,
)


def _client(ctx: Context) -> FmdMcpClient:
    """Retrieve the shared client from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


def _config(ctx: Context) -> Config:
    return ctx.request_context.lifespan_context["config"]


def _unwrap_error(exc: Exception, message: str) -> ErrorResponse:
    log.warning("%s: %s", message, exc)
    return ErrorResponse(error=f"{message}: {exc}")


def _json_resource(payload: Any) -> str:
    """Serialise a resource payload as JSON."""
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Location tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_location_history(
    ctx: Context,
    count: int = 10,
) -> LocationHistoryResponse | ErrorResponse:
    """Get recent location history for the device.

    Args:
        count: Number of recent locations to retrieve (default 10, max 100).
    """
    if count > 100:
        count = 100
    if count < 1:
        count = 1

    client = _client(ctx)
    try:
        locations_data = await client.get_locations(num_to_get=count)
        locations = [Location(**loc) for loc in locations_data]
        device_id = _config(ctx).device_id or _config(ctx).username
        return LocationHistoryResponse(
            locations=locations, count=len(locations), device_id=device_id
        )
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to get location history")


@mcp.tool()
async def get_current_location(ctx: Context) -> Location | ErrorResponse:
    """Get the most recent known location for the device."""
    client = _client(ctx)
    try:
        loc_data = await client.get_location()
        if loc_data is None:
            return ErrorResponse(error="No location data available")
        return Location(**loc_data)
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to get current location")


@mcp.tool()
async def request_location(
    ctx: Context,
    source: str = "gps",
) -> RequestLocationResponse | ErrorResponse:
    """Request a fresh location fix from the device.

    The device will attempt to get a new location and upload it to the server.
    Use get_current_location or get_location_history after a short delay to retrieve it.

    Args:
        source: Location source to request - "gps" (default), "cell", "wifi", "all", or "last".
    """
    client = _client(ctx)
    valid_sources = {"gps", "cell", "wifi", "all", "last"}
    if source not in valid_sources:
        return ErrorResponse(
            error=f"Invalid source: {source}. Must be one of {', '.join(valid_sources)}"
        )

    try:
        await client.request_location(source)
        return RequestLocationResponse(
            message=f"Location request sent (source: {source}). Check again in a moment.",
            source=source,
        )
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to request location")


@mcp.tool()
async def get_device_location(ctx: Context) -> Location | ErrorResponse:
    """Get the last known location via the Device helper (alternative method)."""
    client = _client(ctx)
    try:
        loc_data = await client.get_device_location()
        if loc_data is None:
            return ErrorResponse(error="No location data available")
        return Location(**loc_data)
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to get device location")


# ---------------------------------------------------------------------------
# Device info tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_device_info(ctx: Context) -> DeviceInfo | ErrorResponse:
    """Get device information (battery, connectivity, etc.)."""
    client = _client(ctx)
    try:
        info = await client.get_device_info()
        return DeviceInfo(**info)
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to get device info")


# ---------------------------------------------------------------------------
# Picture tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_pictures(
    ctx: Context,
    count: int = 5,
) -> list[PictureMetadata] | ErrorResponse:
    """Get recent picture metadata from the device.

    Args:
        count: Number of recent pictures to retrieve (default 5, max 20).
    """
    if count > 20:
        count = 20
    if count < 1:
        count = 1

    client = _client(ctx)
    try:
        metadata = await client.get_pictures(num_to_get=count)
        return [PictureMetadata(**m) for m in metadata]
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to get pictures")


@mcp.tool()
async def take_picture(
    ctx: Context,
    camera: str = "front",
) -> MessageResponse | ErrorResponse:
    """Take a picture with the device camera.

    Args:
        camera: Which camera to use - "front" (default) or "back".
    """
    if camera not in ("front", "back"):
        return ErrorResponse(error="camera must be 'front' or 'back'")

    client = _client(ctx)
    try:
        image_bytes = await client.take_picture(camera)
        if image_bytes is None:
            return ErrorResponse(error="Failed to take picture or no image returned")
        return MessageResponse(
            message=f"Picture taken with {camera} camera ({len(image_bytes)} bytes). "
            "Note: Image data is not returned via MCP; check the FMD web UI or use export_data."
        )
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to take picture")


# ---------------------------------------------------------------------------
# Command tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def play_sound(ctx: Context) -> MessageResponse | ErrorResponse:
    """Play a sound on the device (useful for finding it nearby)."""
    client = _client(ctx)
    try:
        success = await client.play_sound()
        if success:
            return MessageResponse(message="Sound played on device")
        return ErrorResponse(error="Failed to play sound")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to play sound")


@mcp.tool()
async def lock_device(
    ctx: Context,
    message: str | None = None,
) -> MessageResponse | ErrorResponse:
    """Lock the device with an optional message displayed on the lock screen.

    Args:
        message: Optional message to display (quotes and newlines will be sanitized).
    """
    client = _client(ctx)
    try:
        success = await client.lock_device(message)
        if success:
            msg = "Device locked"
            if message:
                msg += f" with message: {message}"
            return MessageResponse(message=msg)
        return ErrorResponse(error="Failed to lock device")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to lock device")


@mcp.tool()
async def set_bluetooth(ctx: Context, enable: bool) -> MessageResponse | ErrorResponse:
    """Enable or disable Bluetooth on the device.

    Args:
        enable: True to enable, False to disable.
    """
    client = _client(ctx)
    try:
        success = await client.set_bluetooth(enable)
        if success:
            return MessageResponse(message=f"Bluetooth {'enabled' if enable else 'disabled'}")
        return ErrorResponse(error="Failed to set Bluetooth")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to set Bluetooth")


@mcp.tool()
async def set_do_not_disturb(
    ctx: Context, enable: bool
) -> MessageResponse | ErrorResponse:
    """Enable or disable Do Not Disturb on the device.

    Args:
        enable: True to enable, False to disable.
    """
    client = _client(ctx)
    try:
        success = await client.set_do_not_disturb(enable)
        if success:
            return MessageResponse(
                message=f"Do Not Disturb {'enabled' if enable else 'disabled'}"
            )
        return ErrorResponse(error="Failed to set Do Not Disturb")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to set Do Not Disturb")


@mcp.tool()
async def set_ringer_mode(
    ctx: Context, mode: str
) -> MessageResponse | ErrorResponse:
    """Set the device ringer mode.

    Args:
        mode: One of "normal", "vibrate", "silent".
    """
    if mode not in ("normal", "vibrate", "silent"):
        return ErrorResponse(error="mode must be 'normal', 'vibrate', or 'silent'")

    client = _client(ctx)
    try:
        success = await client.set_ringer_mode(mode)
        if success:
            return MessageResponse(message=f"Ringer mode set to {mode}")
        return ErrorResponse(error="Failed to set ringer mode")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to set ringer mode")


# ---------------------------------------------------------------------------
# Export tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def export_data(ctx: Context, output_path: str) -> MessageResponse | ErrorResponse:
    """Export all locations and pictures to a ZIP file on the server filesystem.

    Args:
        output_path: Path where the ZIP file should be saved (server-side).
    """
    client = _client(ctx)
    try:
        success = await client.export_data(output_path)
        if success:
            return MessageResponse(message=f"Data exported to {output_path}")
        return ErrorResponse(error="Failed to export data")
    except FmdMcpError as exc:
        return _unwrap_error(exc, "Failed to export data")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("fmd://device")
async def device_resource(ctx: Context) -> str:
    """Device information."""
    client = _client(ctx)
    try:
        info = await client.get_device_info()
        return _json_resource(info)
    except FmdMcpError as exc:
        return _json_resource({"error": str(exc)})


@mcp.resource("fmd://location/current")
async def current_location_resource(ctx: Context) -> str:
    """Most recent location."""
    client = _client(ctx)
    try:
        loc = await client.get_location()
        if loc is None:
            return _json_resource({"error": "No location data available"})
        return _json_resource(loc)
    except FmdMcpError as exc:
        return _json_resource({"error": str(exc)})


@mcp.resource("fmd://location/history/{count}")
async def location_history_resource(ctx: Context, count: int = 10) -> str:
    """Recent location history."""
    if count > 100:
        count = 100
    client = _client(ctx)
    try:
        locations = await client.get_locations(num_to_get=count)
        return _json_resource(locations)
    except FmdMcpError as exc:
        return _json_resource({"error": str(exc)})


@mcp.resource("fmd://pictures/{count}")
async def pictures_resource(ctx: Context, count: int = 5) -> str:
    """Recent picture metadata."""
    if count > 20:
        count = 20
    client = _client(ctx)
    try:
        metadata = await client.get_pictures(num_to_get=count)
        return _json_resource(metadata)
    except FmdMcpError as exc:
        return _json_resource({"error": str(exc)})


#: The MCP server object. Import this to run or embed the server.
__all__ = ["mcp"]