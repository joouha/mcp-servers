"""CalDAV MCP Server.

An MCP server for managing calendar events via a CalDAV server.
"""

from __future__ import annotations

import os
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from caldav import get_davclient
from caldav.lib.error import NotFoundError
from pydantic import BaseModel, Field
from fastmcp import Context, FastMCP

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CalendarInfo(BaseModel):
    """A calendar available on the CalDAV server."""

    name: str
    url: str


class EventSummary(BaseModel):
    """Compact event summary for list/search results."""

    uid: str
    summary: str = ""
    start: str | None = None
    end: str | None = None
    recurring: bool = False


class EventDetail(BaseModel):
    """Full event detail returned by get_event."""

    uid: str
    summary: str = ""
    description: str = ""
    location: str = ""
    start: str | None = None
    end: str | None = None
    duration_minutes: int | None = None
    recurring: bool = False
    recurrence: str | None = None
    attendees: list[str] = Field(default_factory=list, description="Participant emails")
    organizer: str | None = None


class EventCreatedResponse(BaseModel):
    """Response after creating an event."""

    uid: str
    message: str


class EventUpdatedResponse(BaseModel):
    """Response after updating an event."""

    message: str


class EventDeletedResponse(BaseModel):
    """Response after deleting an event."""

    message: str


class CalDAVError(BaseModel):
    """Structured error response."""

    error: str


# ---------------------------------------------------------------------------
# CalDAV HTTP client
# ---------------------------------------------------------------------------


class CalDAVClient:
    """Authenticated client for a CalDAV server."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_url: str,
        timezone: str = "UTC",
    ) -> None:
        self.url = url
        self.username = username
        self.password = password
        self.calendar_url = calendar_url
        self.timezone = timezone
        self._client: caldav.DAVClient | None = None
        self._calendar: caldav.Calendar | None = None

    @property
    def client(self) -> caldav.DAVClient:
        """Provides an authenticated caldav client."""
        if self._client is None:
            if not self.url or not self.username or not self.password:
                raise ValueError("CalDAV URL or credentials not configured")
            self._client = get_davclient(
                url=self.url, username=self.username, password=self.password
            )
        return self._client

    @property
    def calendar(self) -> caldav.Calendar:
        """Gets the specific calendar object to work with.

        If a calendar URL is configured, accesses it directly via the
        principal's calendars list.  Otherwise returns the first available
        calendar.
        """
        if self._calendar is None:
            principal = self.client.get_principal()
            calendars = principal.get_calendars()
            if not calendars:
                raise RuntimeError("No calendars found on the server")

            if self.calendar_url:
                # Match by URL
                for cal in calendars:
                    if str(cal.url).rstrip("/") == self.calendar_url.rstrip("/"):
                        self._calendar = cal
                        break
                if self._calendar is None:
                    raise RuntimeError(
                        f"Calendar URL '{self.calendar_url}' not found. "
                        f"Available: {[str(c.url) for c in calendars]}"
                    )
            else:
                self._calendar = calendars[0]
        return self._calendar

    def list_calendars(self) -> list[CalendarInfo]:
        """List all calendars available on the server."""
        principal = self.client.get_principal()
        calendars = principal.get_calendars()
        return [
            CalendarInfo(
                name=str(cal.get_display_name()),
                url=str(cal.url),
            )
            for cal in calendars
        ]

    def list_events(self) -> list[EventSummary]:
        """List events from the configured calendar (±1 year from now)."""
        now = datetime.now(tz=ZoneInfo(self.timezone))
        start = now - timedelta(days=365)
        end = now + timedelta(days=365)
        return self.search_events(start=start, end=end)

    def search_events(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[EventSummary]:
        """Search events within a date range."""
        if start is None:
            start = datetime.now(tz=ZoneInfo(self.timezone))
        if end is None:
            end = start + timedelta(days=30)
        if end <= start:
            start, end = end, start

        caldav_events = self.calendar.search(
            start=start,
            end=end,
            event=True,
            expand=True,
        )
        results: list[EventSummary] = []
        for event in caldav_events:
            ical = event.get_icalendar_component()
            if ical:
                results.append(self._ical_to_summary(ical))
        return results

    def get_event(self, uid: str) -> EventDetail | None:
        """Get full details of a single event by UID."""
        try:
            caldav_event = self.calendar.get_event_by_uid(uid)
            ical = caldav_event.get_icalendar_component()
            return self._ical_to_detail(ical)
        except NotFoundError:
            return None
        except Exception as e:
            log.error("Error loading CalDAV event UID %s: %s", uid, e)
            return None

    def create_event(
        self,
        summary: str,
        start: datetime,
        duration_minutes: int | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        recurrence: str | None = None,
        attendees: list[str] | None = None,
    ) -> EventCreatedResponse:
        """Create a new event on the calendar using keyword arguments."""
        kwargs: dict[str, Any] = {
            "summary": summary,
            "dtstart": start,
        }
        if end is not None:
            kwargs["dtend"] = end
        elif duration_minutes is not None:
            kwargs["dtend"] = start + timedelta(minutes=duration_minutes)

        if description:
            kwargs["description"] = description
        if location:
            kwargs["location"] = location
        if recurrence:
            kwargs["rrule"] = recurrence

        event = self.calendar.add_event(**kwargs)
        uid = str(event.get_icalendar_component().get("uid", ""))
        return EventCreatedResponse(
            uid=uid,
            message=f"Event '{summary}' created successfully",
        )

    def update_event(
        self,
        uid: str,
        summary: str | None = None,
        start: datetime | None = None,
        duration_minutes: int | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        recurrence: str | None = None,
        attendees: list[str] | None = None,
    ) -> EventUpdatedResponse | CalDAVError:
        """Update an existing event on the calendar."""
        try:
            caldav_event = self.calendar.get_event_by_uid(uid)
        except NotFoundError:
            return CalDAVError(error=f"Event {uid} not found")

        with caldav_event.edit_icalendar_component() as ical:
            if summary is not None:
                ical["summary"] = summary

            if start is not None:
                if "dtstart" in ical:
                    del ical["dtstart"]
                ical.add("dtstart", start)

            if end is not None:
                if "dtend" in ical:
                    del ical["dtend"]
                if "duration" in ical:
                    del ical["duration"]
                ical.add("dtend", end)
            elif duration_minutes is not None:
                if "dtend" in ical:
                    del ical["dtend"]
                if "duration" in ical:
                    del ical["duration"]
                dt_start = start or ical.decoded("dtstart")
                ical.add("dtend", dt_start + timedelta(minutes=duration_minutes))

            if description is not None:
                if "description" in ical:
                    del ical["description"]
                if description:
                    ical.add("description", description)

            if location is not None:
                if "location" in ical:
                    del ical["location"]
                if location:
                    ical.add("location", location)

            if recurrence is not None:
                if "rrule" in ical:
                    del ical["rrule"]
                if recurrence:
                    from icalendar.prop import vRecur

                    try:
                        ical.add("rrule", vRecur.from_ical(recurrence))
                    except ValueError:
                        log.warning("Invalid recurrence rule: %s", recurrence)

        caldav_event.save()
        return EventUpdatedResponse(message=f"Event {uid} updated successfully")

    def delete_event(self, uid: str) -> EventDeletedResponse | CalDAVError:
        """Delete an event from the calendar."""
        try:
            caldav_event = self.calendar.get_event_by_uid(uid)
            caldav_event.delete()
            return EventDeletedResponse(message=f"Event {uid} deleted successfully")
        except NotFoundError:
            return CalDAVError(error=f"Event {uid} not found (already deleted?)")
        except Exception as e:
            log.error("Error deleting CalDAV event UID %s: %s", uid, e)
            raise

    # -- helpers ------------------------------------------------------------

    def _parse_dt(self, value: Any) -> str | None:
        """Parse a date or datetime to ISO string."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    def _ical_to_summary(self, ical: Any) -> EventSummary:
        """Convert an icalendar Event component to a compact summary."""
        uid = str(ical.get("uid", ""))
        summary_val = str(ical.get("summary", ""))

        start = ical.decoded("dtstart") if "dtstart" in ical else None
        end = ical.decoded("dtend") if "dtend" in ical else None

        return EventSummary(
            uid=uid,
            summary=summary_val,
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            recurring="rrule" in ical,
        )

    def _ical_to_detail(self, ical: Any) -> EventDetail:
        """Convert an icalendar Event component to full detail."""
        uid = str(ical.get("uid", ""))
        summary_val = str(ical.get("summary", ""))
        description_val = str(ical.get("description", ""))
        location_val = str(ical.get("location", ""))

        start = ical.decoded("dtstart") if "dtstart" in ical else None
        end = ical.decoded("dtend") if "dtend" in ical else None

        duration = None
        if start and end:
            if isinstance(start, datetime) and isinstance(end, datetime):
                duration = int((end - start).total_seconds() / 60)
            elif isinstance(start, date) and isinstance(end, date):
                duration = (end - start).days * 24 * 60

        recurrence = None
        if rrule := ical.get("rrule"):
            recurrence = rrule.to_ical().decode()

        attendees: list[str] = []
        raw_attendees = ical.get("attendee", [])
        # Single attendee comes back as a scalar, not a list
        if not isinstance(raw_attendees, list):
            raw_attendees = [raw_attendees]
        for attendee in raw_attendees:
            uri = str(attendee)
            _, _, email = uri.partition(":")
            if email:
                attendees.append(email)

        organizer = None
        if org := ical.get("organizer"):
            _, _, organizer = str(org).partition(":")

        return EventDetail(
            uid=uid,
            summary=summary_val,
            description=description_val,
            location=location_val,
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            duration_minutes=duration,
            recurring="rrule" in ical,
            recurrence=recurrence,
            attendees=attendees,
            organizer=organizer,
        )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create a single CalDAVClient for the server's lifetime."""
    url = os.environ.get("CALDAV_URL", "")
    username = os.environ.get("CALDAV_USERNAME", "")
    password = os.environ.get("CALDAV_PASSWORD", "")
    calendar_url = os.environ.get("CALDAV_CALENDAR_URL", "")
    timezone = os.environ.get("CALDAV_TIMEZONE", "UTC")

    if not url:
        msg = "CALDAV_URL environment variable is required"
        raise RuntimeError(msg)
    if not username or not password:
        msg = "CALDAV_USERNAME and CALDAV_PASSWORD environment variables are required"
        raise RuntimeError(msg)

    client = CalDAVClient(
        url=url,
        username=username,
        password=password,
        calendar_url=calendar_url,
        timezone=timezone,
    )
    try:
        yield {"client": client}
    finally:
        pass


mcp = FastMCP(
    "CalDAV",
    instructions=(
        "MCP server for managing calendar events via a CalDAV server. "
        "Use the provided tools to list, search, create, update, and delete "
        "calendar events. Set CALDAV_CALENDAR_URL to work with a specific "
        "calendar, or use list_calendars to discover available calendars."
    ),
    lifespan=lifespan,
)


def _get_client(ctx: Context) -> CalDAVClient:
    """Retrieve the shared CalDAVClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


@mcp.tool()
def list_events(ctx: Context) -> list[EventSummary]:
    """List all events on the configured calendar with summary info."""
    client = _get_client(ctx)
    return client.list_events()


@mcp.tool()
def search_events(
    ctx: Context,
    start: str | None = None,
    end: str | None = None,
    query: str | None = None,
) -> list[EventSummary]:
    """Search events within a date range and/or by text query.

    Args:
        start: Start of date range as ISO 8601 string. Defaults to now.
        end: End of date range as ISO 8601 string. Defaults to 30 days from start.
        query: Optional text to filter results by summary (case-insensitive).
    """
    client = _get_client(ctx)
    tz = ZoneInfo(client.timezone)

    start_dt = datetime.fromisoformat(start).replace(tzinfo=tz) if start else None
    end_dt = datetime.fromisoformat(end).replace(tzinfo=tz) if end else None

    results = client.search_events(start=start_dt, end=end_dt)

    if query:
        q = query.lower()
        results = [r for r in results if q in r.summary.lower()]

    return results


@mcp.tool()
def get_event(ctx: Context, uid: str) -> EventDetail | CalDAVError:
    """Get full details of a single calendar event.

    Args:
        uid: The UID of the event to retrieve.
    """
    client = _get_client(ctx)
    event = client.get_event(uid)
    if event is None:
        return CalDAVError(error=f"Event {uid} not found")
    return event


@mcp.tool()
def create_event(
    ctx: Context,
    summary: str,
    start: str,
    duration_minutes: int | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    recurrence: str | None = None,
    attendees: list[str] | None = None,
) -> EventCreatedResponse:
    """Create a new calendar event.

    Args:
        summary: Title/summary of the event.
        start: Start time as ISO 8601 string (e.g. "2025-01-15T10:00:00").
        duration_minutes: Duration in minutes. Either this or end must be provided.
        end: End time as ISO 8601 string. Either this or duration_minutes must be provided.
        description: Optional description of the event.
        location: Optional location of the event.
        recurrence: Optional iCalendar RRULE string (e.g. "FREQ=WEEKLY;BYDAY=MO,WE,FR").
        attendees: Optional list of attendee email addresses.
    """
    client = _get_client(ctx)
    tz = ZoneInfo(client.timezone)

    start_dt = datetime.fromisoformat(start)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=tz)

    end_dt = None
    if end is not None:
        end_dt = datetime.fromisoformat(end)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=tz)

    return client.create_event(
        summary=summary,
        start=start_dt,
        duration_minutes=duration_minutes,
        end=end_dt,
        description=description,
        location=location,
        recurrence=recurrence,
        attendees=attendees,
    )


@mcp.tool()
def update_event(
    ctx: Context,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    duration_minutes: int | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    recurrence: str | None = None,
    attendees: list[str] | None = None,
) -> EventUpdatedResponse | CalDAVError:
    """Update an existing calendar event. Only provided fields are changed.

    Args:
        uid: The UID of the event to update.
        summary: New title/summary.
        start: New start time as ISO 8601 string.
        duration_minutes: New duration in minutes.
        end: New end time as ISO 8601 string.
        description: New description (empty string to clear).
        location: New location (empty string to clear).
        recurrence: New RRULE string (empty string to clear).
        attendees: New list of attendee emails (replaces existing attendees).
    """
    client = _get_client(ctx)
    tz = ZoneInfo(client.timezone)

    start_dt = None
    if start is not None:
        start_dt = datetime.fromisoformat(start)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=tz)

    end_dt = None
    if end is not None:
        end_dt = datetime.fromisoformat(end)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=tz)

    return client.update_event(
        uid=uid,
        summary=summary,
        start=start_dt,
        duration_minutes=duration_minutes,
        end=end_dt,
        description=description,
        location=location,
        recurrence=recurrence,
        attendees=attendees,
    )


@mcp.tool()
def delete_event(ctx: Context, uid: str) -> EventDeletedResponse | CalDAVError:
    """Delete a calendar event.

    Args:
        uid: The UID of the event to delete.
    """
    client = _get_client(ctx)
    return client.delete_event(uid)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the CalDAV MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
