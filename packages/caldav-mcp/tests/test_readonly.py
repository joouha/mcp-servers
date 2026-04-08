"""Read-only smoke tests for the CalDAV MCP server.

Assumes CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD, and optionally
CALDAV_CALENDAR_URL and CALDAV_TIMEZONE are set in the environment.

Run with:
    uv run --package caldav-mcp python packages/caldav-mcp/tests/test_readonly.py
"""

from __future__ import annotations

import json
import sys

from caldav_mcp import (
    CalDAVClient,
    CalendarInfo,
    EventDetail,
    EventSummary,
    CalDAVError,
)


def heading(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def dump(label: str, obj: object) -> None:
    """Pretty-print a pydantic model or list of models."""
    if isinstance(obj, list):
        print(f"\n{label} ({len(obj)} items):")
        for item in obj:
            if hasattr(item, "model_dump"):
                print(f"  {json.dumps(item.model_dump(), indent=2, default=str)}")
            else:
                print(f"  {item}")
    elif hasattr(obj, "model_dump"):
        print(f"\n{label}:")
        print(f"  {json.dumps(obj.model_dump(), indent=2, default=str)}")
    else:
        print(f"\n{label}: {obj}")


def test_client_direct() -> None:
    """Test the CalDAVClient directly (not through MCP)."""
    import os

    heading("Direct CalDAVClient tests")

    url = os.environ.get("CALDAV_URL", "")
    username = os.environ.get("CALDAV_USERNAME", "")
    password = os.environ.get("CALDAV_PASSWORD", "")
    calendar_url = os.environ.get("CALDAV_CALENDAR_URL", "")
    timezone = os.environ.get("CALDAV_TIMEZONE", "UTC")

    if not url or not username or not password:
        print("SKIP: CALDAV_URL/USERNAME/PASSWORD not set")
        return

    client = CalDAVClient(
        url=url,
        username=username,
        password=password,
        calendar_url=calendar_url,
        timezone=timezone,
    )

    # 1. List calendars
    calendars = client.list_calendars()
    dump("Calendars", calendars)
    assert isinstance(calendars, list)
    for cal in calendars:
        assert isinstance(cal, CalendarInfo)
        assert cal.name
        assert cal.url
    print("✓ list_calendars returns list[CalendarInfo]")

    # 2. List events
    events = client.list_events()
    dump("Events (sample of first 5)", events[:5])
    assert isinstance(events, list)
    for ev in events:
        assert isinstance(ev, EventSummary)
        assert ev.uid
    print(f"✓ list_events returns list[EventSummary] ({len(events)} events)")

    # 3. Search events (next 30 days)
    search_results = client.search_events()
    dump("Search results (next 30 days, first 5)", search_results[:5])
    assert isinstance(search_results, list)
    for ev in search_results:
        assert isinstance(ev, EventSummary)
    print(f"✓ search_events returns list[EventSummary] ({len(search_results)} events)")

    # 4. Get event detail (if any events exist)
    if events:
        uid = events[0].uid
        detail = client.get_event(uid)
        dump(f"Event detail (uid={uid})", detail)
        assert detail is None or isinstance(detail, EventDetail)
        if isinstance(detail, EventDetail):
            assert detail.uid == uid
            print(f"✓ get_event returns EventDetail for uid={uid}")
        else:
            print(f"⚠ get_event returned None for uid={uid}")

    # 5. Get non-existent event
    missing = client.get_event("nonexistent-uid-12345")
    assert missing is None
    print("✓ get_event returns None for missing UID")


def main() -> None:
    errors = 0

    try:
        test_client_direct()
    except Exception as e:
        print(f"\n✗ Direct client test failed: {e}")
        errors += 1

    heading("Summary")
    if errors:
        print(f"  {errors} test group(s) FAILED")
        sys.exit(1)
    else:
        print("  All tests PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
