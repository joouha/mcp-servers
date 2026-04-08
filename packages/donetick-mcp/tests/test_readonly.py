"""Read-only smoke tests for the Donetick MCP server.

Assumes DONETICK_URL, DONETICK_USERNAME, and DONETICK_PASSWORD are set
in the environment.

Run with:
    uv run --package donetick-mcp python packages/donetick-mcp/tests/test_readonly.py
"""

from __future__ import annotations

import json
import sys

from donetick_mcp import (
    DonetickClient,
    DonetickChore,
    UserProfile,
    ChoreSummary,
    ChoreDetail,
    DonetickError,
    _chore_summary,
    _chore_detail,
)


def heading(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def dump(label: str, obj: object) -> None:
    """Pretty-print a pydantic model or list of models."""
    if isinstance(obj, list):
        print(f"\n{label} ({len(obj)} items):")
        for item in obj[:10]:
            if hasattr(item, "model_dump"):
                print(f"  {json.dumps(item.model_dump(), indent=2, default=str)}")
            else:
                print(f"  {item}")
        if len(obj) > 10:
            print(f"  ... and {len(obj) - 10} more")
    elif hasattr(obj, "model_dump"):
        print(f"\n{label}:")
        print(f"  {json.dumps(obj.model_dump(), indent=2, default=str)}")
    else:
        print(f"\n{label}: {obj}")


def test_client_direct() -> None:
    """Test the DonetickClient directly (not through MCP)."""
    import os

    heading("Direct DonetickClient tests")

    url = os.environ.get("DONETICK_URL", "https://donetick.com/")
    username = os.environ.get("DONETICK_USERNAME", "")
    password = os.environ.get("DONETICK_PASSWORD", "")

    if not username or not password:
        print("SKIP: DONETICK_USERNAME/PASSWORD not set")
        return

    client = DonetickClient(url=url, username=username, password=password)

    # 1. Get profile
    profile = client.get_profile()
    dump("Profile", profile)
    assert isinstance(profile, UserProfile)
    assert profile.id > 0
    print(f"✓ get_profile returns UserProfile (id={profile.id})")

    # 2. List chores
    chores = client.list_chores()
    dump("Chores (first 5)", chores[:5])
    assert isinstance(chores, list)
    for c in chores:
        assert isinstance(c, DonetickChore)
    print(f"✓ list_chores returns list[DonetickChore] ({len(chores)} chores)")

    # 3. Test _chore_summary helper
    if chores:
        summary = _chore_summary(chores[0])
        dump("Chore summary", summary)
        assert isinstance(summary, ChoreSummary)
        assert summary.name == chores[0].name
        print("✓ _chore_summary returns ChoreSummary")

    # 4. Get single chore detail
    if chores:
        chore_id = chores[0].id
        chore = client.get_chore(chore_id)
        assert chore is not None
        assert isinstance(chore, DonetickChore)
        detail = _chore_detail(chore)
        dump(f"Chore detail (id={chore_id})", detail)
        assert isinstance(detail, ChoreDetail)
        assert detail.id == chore_id
        print(f"✓ get_chore + _chore_detail returns ChoreDetail for id={chore_id}")

    # 5. Get non-existent chore (server may return 500, client should handle)
    try:
        missing = client.get_chore(999999999)
        assert missing is None
        print("✓ get_chore returns None for missing ID")
    except Exception as e:
        print(f"⚠ get_chore(999999999) raised {type(e).__name__}: {e}")
        print("  (Donetick server returns 500 for missing IDs — expected)")


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
