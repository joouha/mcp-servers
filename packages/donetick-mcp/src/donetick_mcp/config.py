"""Configuration for the Donetick MCP server.

All settings come from environment variables so the server can be configured
purely through an MCP client's ``env`` block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Environment variable names.
ENV_URL = "DONETICK_URL"
ENV_USERNAME = "DONETICK_USERNAME"
ENV_PASSWORD = "DONETICK_PASSWORD"
ENV_TIMEOUT = "DONETICK_TIMEOUT"
ENV_TIMEZONE = "DONETICK_TIMEZONE"
ENV_RATE_LIMIT = "DONETICK_RATE_LIMIT_PER_SECOND"
ENV_RATE_BURST = "DONETICK_RATE_LIMIT_BURST"
ENV_MAX_RETRIES = "DONETICK_MAX_RETRIES"
ENV_VERIFY_TLS = "DONETICK_VERIFY_TLS"
ENV_LOG_LEVEL = "DONETICK_LOG_LEVEL"


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved server configuration."""

    url: str
    username: str
    password: str
    timeout: float
    timezone: str
    rate_limit_per_second: float
    rate_limit_burst: int
    max_retries: int
    verify_tls: bool
    log_level: str

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
            url=_normalise_url(os.environ.get(ENV_URL, "https://donetick.com/")),
            username=username,
            password=password,
            timeout=_positive_float(ENV_TIMEOUT, 10.0),
            timezone=os.environ.get(ENV_TIMEZONE, "UTC") or "UTC",
            rate_limit_per_second=_positive_float(ENV_RATE_LIMIT, 10.0),
            rate_limit_burst=_positive_int(ENV_RATE_BURST, 10),
            max_retries=_positive_int(ENV_MAX_RETRIES, 3),
            verify_tls=_bool(ENV_VERIFY_TLS, True),
            log_level=os.environ.get(ENV_LOG_LEVEL, "WARNING").upper(),
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


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}"
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