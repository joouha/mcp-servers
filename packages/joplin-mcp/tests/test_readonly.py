"""Read-only smoke tests for the Joplin MCP server.

Requires JOPLIN_SERVER_URL, JOPLIN_EMAIL, and JOPLIN_PASSWORD to be set.
Exercises list/search/get operations without creating or modifying data.
"""

from __future__ import annotations

import json
import sys

from joplin_mcp import JoplinClient


def dump(label: str, obj: object) -> None:
    """Pretty-print a labelled object."""
    if isinstance(obj, list):
        data = [
            item.model_dump() if hasattr(item, "model_dump") else item for item in obj
        ]
    elif hasattr(obj, "model_dump"):
        data = obj.model_dump()
    else:
        data = obj
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(json.dumps(data, indent=2, default=str))


async def _run() -> None:
    import os

    url = os.environ.get("JOPLIN_SERVER_URL", "")
    email = os.environ.get("JOPLIN_EMAIL", "")
    password = os.environ.get("JOPLIN_PASSWORD", "")

    if not url or not email or not password:
        print(
            "Set JOPLIN_SERVER_URL, JOPLIN_EMAIL, and JOPLIN_PASSWORD "
            "environment variables to run this test."
        )
        sys.exit(1)

    client = JoplinClient(url=url, email=email, password=password)

    try:
        notebooks = await client.list_notebooks()
        dump("list_notebooks()", notebooks)

        if notebooks:
            nb = notebooks[0]
            dump(f"get_notebook({nb.id})", await client.get_notebook(nb.id))

        notes = await client.list_notes(limit=5)
        dump("list_notes(limit=5)", notes)

        if notes:
            note = notes[0]
            dump(f"get_note({note.id})", await client.get_note(note.id))

            tags = await client.get_note_tags(note.id)
            dump(f"get_note_tags({note.id})", tags)

        results = await client.search_notes("test", limit=5)
        dump("search_notes('test', limit=5)", results)

        all_tags = await client.list_tags()
        dump("list_tags()", all_tags)
    finally:
        await client.close()


def main() -> None:
    import asyncio

    asyncio.run(_run())


if __name__ == "__main__":
    main()
