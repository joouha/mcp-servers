"""Pydantic models for FMD MCP responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Location(BaseModel):
    """Decrypted location data from FMD."""

    lat: float = Field(..., description="Latitude in degrees")
    lon: float = Field(..., description="Longitude in degrees")
    accuracy: float | None = Field(None, description="Accuracy in meters")
    altitude: float | None = Field(None, description="Altitude in meters")
    speed: float | None = Field(None, description="Speed in m/s")
    bearing: float | None = Field(None, description="Bearing in degrees")
    timestamp: datetime | None = Field(None, description="When the location was recorded")
    provider: str | None = Field(None, description="Location provider (gps, network, etc.)")

    # Allow extra fields from the decrypted JSON
    model_config = {"extra": "allow"}


class DeviceInfo(BaseModel):
    """Device information from FMD."""

    device_id: str
    last_seen: datetime | str | None = None
    battery: int | None = Field(None, description="Battery level percentage")
    wifi: str | None = None
    bluetooth: bool | None = None
    do_not_disturb: bool | None = None
    ringer_mode: str | None = None
    error: str | None = None

    model_config = {"extra": "allow"}


class PictureMetadata(BaseModel):
    """Picture metadata from FMD."""

    id: str | int | None = None
    date: datetime | str | None = None
    filename: str | None = None
    size: int | None = None

    model_config = {"extra": "allow"}


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str


class MessageResponse(BaseModel):
    """Simple success message response."""

    message: str


class LocationHistoryResponse(BaseModel):
    """Response for location history queries."""

    locations: list[Location]
    count: int
    device_id: str


class RequestLocationResponse(BaseModel):
    """Response for location request."""

    message: str
    source: str