"""Configuration for the FMD MCP server.

All settings come from environment variables so the server can be configured
purely through an MCP client's ``env`` block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


#: Environment variable names.
ENV_URL = "FMD_URL"
ENV_USERNAME = "FMD_USERNAME"
ENV_PASSWORD = "FMD_PASSWORD"
ENV_TIMEOUT = "FMD_TIMEOUT"
ENV_VERIFY_TLS = "FMD_VERIFY_TLS"
ENV_LOG_LEVEL = "FMD_LOG_LEVEL"
ENV_DEVICE_ID = "FMD_DEVICE_ID"  # Optional: specific device to query


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved server configuration."""

    url: str
    username: str
    password: str
    timeout: float
    verify_tls: bool
    log_level: str
    device_id: str | None

    @classmethod
    def from_env(cls) -> Config:
        """Build a config from environment variables.

        Raises:
            RuntimeError: If required credentials are missing or a numeric
                setting cannot be parsed.
        """
        username = os.environ.get(ENV_USERNAME, "")
        password = os.environ.get(ENV_PASSWORD, "")
        missing = [
            name
            for name, value in ((ENV_USERNAME, username), (ENV_PASSWORD, password))
            if not value
        ]
        if missing:
            msg = f"{' and '.join(missing)} environment variable(s) are required"
            raise RuntimeError(msg)

        return cls(
            url=_normalise_url(os.environ.get(ENV_URL, "https://server.fmd-foss.org/")),
            username=username,
            password=password,
            timeout=_positive_float(ENV_TIMEOUT, 30.0),
            verify_tls=_bool(ENV_VERIFY_TLS, True),
            log_level=os.environ.get(ENV_LOG_LEVEL, "WARNING").upper(),
            device_id=os.environ.get(ENV_DEVICE_ID) or None,
        )


def _normalise_url(url: str) -> str:
    """Strip trailing slashes so path joins behave predictably."""
    return url.rstrip("/")


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{name} must be a number, got {raw!r}"
        raise RuntimeError(msg) from exc
    if value <= 0:
        msg = f"{name} must be greater than 0, got {value}"
        raise RuntimeError(msg)
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    msg = f"{name} must be a boolean, got {raw!r}"
    raise RuntimeError(msg)