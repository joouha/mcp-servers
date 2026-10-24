"""FMD API client wrapper with connection lifecycle management."""

from __future__ import annotations

import logging
import ssl
from typing import Any

from fmd_api import FmdClient, Device
from fmd_api.exceptions import AuthenticationError, FmdApiException

from .config import Config

log = logging.getLogger(__name__)


class FmdMcpError(Exception):
    """Base exception for FMD MCP errors."""

    pass


class FmdMcpAuthError(FmdMcpError):
    """Authentication failed."""

    pass


class FmdMcpNotFoundError(FmdMcpError):
    """Resource not found."""

    pass


class FmdMcpClient:
    """Wrapper around fmd_api.FmdClient with MCP-friendly error handling."""

    def __init__(self, config: Config):
        self._config = config
        self._client: FmdClient | None = None
        self._device: Device | None = None
        self._device_id = config.device_id

    @property
    def client(self) -> FmdClient:
        if self._client is None:
            raise FmdMcpError("Client not initialized. Call ensure_auth() first.")
        return self._client

    @property
    def device(self) -> Device:
        if self._device is None:
            raise FmdMcpError("Device not initialized. Call ensure_auth() first.")
        return self._device

    def _build_ssl_context(self) -> ssl.SSLContext | bool:
        """Build SSL context based on config."""
        if not self._config.verify_tls:
            return False
        # Default context with system CAs
        return ssl.create_default_context()

    async def ensure_auth(self) -> None:
        """Authenticate and initialize the client and device."""
        ssl_ctx = self._build_ssl_context()

        self._client = await FmdClient.create(
            self._config.url,
            self._config.username,
            self._config.password,
            ssl=ssl_ctx,
            drop_password=True,  # Don't keep password in memory
        )

        # If device_id is specified, use that; otherwise use the username as device ID
        device_id = self._device_id or self._config.username
        self._device = Device(self._client, device_id)

        # Test the connection by refreshing device state
        try:
            await self._device.refresh()
        except AuthenticationError as exc:
            raise FmdMcpAuthError(f"Authentication failed: {exc}") from exc
        except FmdApiException as exc:
            raise FmdMcpError(f"Failed to connect to FMD server: {exc}") from exc

        log.info("Authenticated to FMD server at %s", self._config.url)

    async def aclose(self) -> None:
        """Close the client session."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._device = None

    # --- Location methods ---

    async def get_locations(self, num_to_get: int = 10) -> list[dict[str, Any]]:
        """Fetch and decrypt recent locations.

        Returns a list of parsed location dicts with keys: lat, lon, accuracy, timestamp, etc.
        """
        if self._client is None:
            raise FmdMcpError("Client not initialized")

        blobs = await self._client.get_locations(num_to_get=num_to_get)
        locations = []
        for blob in blobs:
            try:
                decrypted = self._client.decrypt_data_blob(blob)
                import json

                loc = json.loads(decrypted.decode("utf-8"))
                locations.append(loc)
            except Exception as exc:
                log.warning("Failed to decrypt location blob: %s", exc)
                continue
        return locations

    async def get_location(self) -> dict[str, Any] | None:
        """Get the most recent location (parsed)."""
        locations = await self.get_locations(num_to_get=1)
        return locations[0] if locations else None

    async def request_location(self, source: str = "gps") -> None:
        """Request a fresh location fix from the device.

        Args:
            source: One of "all", "gps", "cell", "last"
        """
        if self._client is None:
            raise FmdMcpError("Client not initialized")

        valid_sources = {"all", "gps", "cell", "last"}
        if source not in valid_sources:
            raise ValueError(f"Invalid source: {source}. Must be one of {valid_sources}")

        await self._client.request_location(source)

    async def get_device_location(self) -> dict[str, Any] | None:
        """Get the last known location via the Device helper."""
        try:
            return await self.device.get_location()
        except Exception as exc:
            log.warning("Failed to get device location: %s", exc)
            return None

    # --- Device info ---

    async def get_device_info(self) -> dict[str, Any]:
        """Get device information."""
        try:
            await self.device.refresh()
            # Device object has various attributes
            return {
                "device_id": self._device_id or self._config.username,
                "last_seen": getattr(self.device, "last_seen", None),
                "battery": getattr(self.device, "battery", None),
                "wifi": getattr(self.device, "wifi", None),
                "bluetooth": getattr(self.device, "bluetooth", None),
                "do_not_disturb": getattr(self.device, "do_not_disturb", None),
                "ringer_mode": getattr(self.device, "ringer_mode", None),
            }
        except Exception as exc:
            log.warning("Failed to get device info: %s", exc)
            return {"device_id": self._device_id or self._config.username, "error": str(exc)}

    # --- Picture methods ---

    async def get_pictures(self, num_to_get: int = 5) -> list[dict[str, Any]]:
        """Fetch recent picture metadata."""
        try:
            metadata = await self.device.get_picture_metadata(num_to_get)
            return metadata
        except Exception as exc:
            log.warning("Failed to get picture metadata: %s", exc)
            return []

    async def take_picture(self, camera: str = "front") -> bytes | None:
        """Take a picture and return the decoded image bytes.

        Args:
            camera: "front" or "back"
        """
        if camera not in ("front", "back"):
            raise ValueError("camera must be 'front' or 'back'")

        try:
            if camera == "front":
                await self.device.take_front_picture()
            else:
                await self.device.take_rear_picture()

            # Fetch the latest picture
            blobs = await self.device.get_picture_blobs(1)
            if blobs:
                return await self.device.decode_picture(blobs[0])
        except Exception as exc:
            log.warning("Failed to take picture: %s", exc)
        return None

    # --- Commands ---

    async def play_sound(self) -> bool:
        """Play a sound on the device."""
        try:
            await self.device.play_sound()
            return True
        except Exception as exc:
            log.warning("Failed to play sound: %s", exc)
            return False

    async def lock_device(self, message: str | None = None) -> bool:
        """Lock the device with an optional message."""
        try:
            await self.device.lock(message=message)
            return True
        except Exception as exc:
            log.warning("Failed to lock device: %s", exc)
            return False

    async def set_bluetooth(self, enable: bool) -> bool:
        """Enable or disable Bluetooth."""
        try:
            await self._client.set_bluetooth(enable)
            return True
        except Exception as exc:
            log.warning("Failed to set bluetooth: %s", exc)
            return False

    async def set_do_not_disturb(self, enable: bool) -> bool:
        """Enable or disable Do Not Disturb."""
        try:
            await self._client.set_do_not_disturb(enable)
            return True
        except Exception as exc:
            log.warning("Failed to set do not disturb: %s", exc)
            return False

    async def set_ringer_mode(self, mode: str) -> bool:
        """Set ringer mode: normal, vibrate, silent."""
        if mode not in ("normal", "vibrate", "silent"):
            raise ValueError("mode must be 'normal', 'vibrate', or 'silent'")
        try:
            await self._client.set_ringer_mode(mode)
            return True
        except Exception as exc:
            log.warning("Failed to set ringer mode: %s", exc)
            return False

    async def export_data(self, output_path: str) -> bool:
        """Export all locations and pictures to a ZIP file."""
        try:
            await self._client.export_data_zip(output_path)
            return True
        except Exception as exc:
            log.warning("Failed to export data: %s", exc)
            return False