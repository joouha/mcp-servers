"""Tandoor MCP Server.

An MCP server for managing meal plans via the Tandoor Recipes API.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import msgspec
from fastmcp import Context, FastMCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tandoor data structures
# ---------------------------------------------------------------------------


class TandoorMealType(msgspec.Struct):
    """A meal type in Tandoor (e.g. breakfast, lunch, dinner)."""

    id: int | None = None
    name: str = ""
    order: int = 0


class TandoorRecipe(msgspec.Struct, kw_only=True):
    """A recipe reference inside a meal plan entry."""

    id: int
    name: str = ""


class TandoorMealPlan(msgspec.Struct, kw_only=True):
    """A meal plan entry in Tandoor."""

    id: int | None = None
    title: str = ""
    recipe: TandoorRecipe | None = None
    servings: float = 1.0
    note: str = ""
    from_date: str = ""
    to_date: str = ""
    meal_type: TandoorMealType | None = None


# ---------------------------------------------------------------------------
# Tandoor HTTP client
# ---------------------------------------------------------------------------


class TandoorClient:
    """Authenticated client for the Tandoor Recipes REST API."""

    def __init__(
        self,
        url: str,
        key: str,
        timeout: int = 20,
        timezone: str = "UTC",
    ) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.timezone = timezone
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Provides an authenticated httpx client."""
        if self._client is None:
            if not self.key:
                raise ValueError("Tandoor API key is not configured")
            self._client = httpx.Client(
                headers={"Authorization": f"Bearer {self.key}"},
                timeout=self.timeout,
                base_url=self.url,
            )
        return self._client

    # -- meal types ---------------------------------------------------------

    def list_meal_types(self) -> list[dict[str, Any]]:
        """List all meal types configured in Tandoor."""
        resp = self.client.get("/api/meal-type/")
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("results", data) if isinstance(data, dict) else data
        meal_types = msgspec.convert(raw, type=list[TandoorMealType])
        return [msgspec.to_builtins(mt) for mt in meal_types]

    # -- recipes ------------------------------------------------------------

    def search_recipes(self, query: str | None = None) -> list[dict[str, Any]]:
        """Search recipes by name.

        Args:
            query: Optional search string to filter recipes by name.
        """
        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        resp = self.client.get("/api/recipe/", params=params)
        resp.raise_for_status()
        data = resp.json()
        # Tandoor paginates recipes; return the results list
        results = data.get("results", data) if isinstance(data, dict) else data
        return results

    # -- meal plans ---------------------------------------------------------

    def list_meal_plans(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """List meal plan entries within a date range.

        Args:
            from_date: Start of range. Defaults to today.
            to_date: End of range. Defaults to 7 days from from_date.
            query: Optional string to filter meal plans by title (case-insensitive).
        """
        if from_date is None:
            from_date = date.today()
        if to_date is None:
            to_date = from_date + timedelta(days=7)

        params = {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        }
        resp = self.client.get("/api/meal-plan/", params=params)
        resp.raise_for_status()
        data = resp.json()
        # Tandoor paginates; results may be in a "results" key or bare list
        raw = data.get("results", data) if isinstance(data, dict) else data
        plans = msgspec.convert(raw, type=list[TandoorMealPlan])
        results = [msgspec.to_builtins(p) for p in plans]
        if query:
            q = query.lower()
            results = [
                r for r in results
                if q in r.get("title", "").lower()
                or q in r.get("note", "").lower()
                or (r.get("recipe") and q in r["recipe"].get("name", "").lower())
            ]
        return results

    def get_meal_plan(self, meal_plan_id: int) -> dict[str, Any] | None:
        """Get a single meal plan entry by ID.

        Args:
            meal_plan_id: The Tandoor meal plan entry ID.
        """
        resp = self.client.get(f"/api/meal-plan/{meal_plan_id}/")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        plan = msgspec.convert(resp.json(), type=TandoorMealPlan)
        return msgspec.to_builtins(plan)

    def create_meal_plan(
        self,
        from_date: str,
        to_date: str | None = None,
        meal_type_id: int | None = None,
        title: str = "",
        recipe_id: int | None = None,
        servings: float = 1.0,
        note: str = "",
    ) -> dict[str, Any]:
        """Create a new meal plan entry.

        Args:
            from_date: Start date as ISO 8601 string (e.g. "2025-01-15").
            to_date: End date as ISO 8601 string. Defaults to from_date.
            meal_type_id: ID of the meal type.
            title: Title/name for the meal plan entry.
            recipe_id: Optional Tandoor recipe ID to link.
            servings: Number of servings.
            note: Optional note/description.
        """
        if to_date is None:
            to_date = from_date

        body: dict[str, Any] = {
            "title": title,
            "from_date": from_date,
            "to_date": to_date,
            "servings": servings,
            "note": note,
        }
        if meal_type_id is not None:
            body["meal_type"] = meal_type_id
        if recipe_id is not None:
            body["recipe"] = recipe_id

        resp = self.client.post("/api/meal-plan/", json=body)
        if resp.status_code == 400:
            return {"error": "Bad request", "details": resp.json()}
        resp.raise_for_status()
        created = msgspec.convert(resp.json(), type=TandoorMealPlan)
        return msgspec.to_builtins(created)

    def update_meal_plan(
        self,
        meal_plan_id: int,
        title: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        meal_type_id: int | None = None,
        recipe_id: int | None = None,
        servings: float | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing meal plan entry. Only provided fields are changed.

        Args:
            meal_plan_id: The Tandoor meal plan entry ID.
            title: New title.
            from_date: New start date as ISO 8601 string.
            to_date: New end date as ISO 8601 string.
            meal_type_id: New meal type ID.
            recipe_id: New recipe ID to link.
            servings: New number of servings.
            note: New note/description.
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if from_date is not None:
            body["from_date"] = from_date
        if to_date is not None:
            body["to_date"] = to_date
        if meal_type_id is not None:
            body["meal_type"] = meal_type_id
        if recipe_id is not None:
            body["recipe"] = recipe_id
        if servings is not None:
            body["servings"] = servings
        if note is not None:
            body["note"] = note

        resp = self.client.patch(f"/api/meal-plan/{meal_plan_id}/", json=body)
        if resp.status_code == 404:
            return {"error": f"Meal plan {meal_plan_id} not found"}
        if resp.status_code == 400:
            return {"error": "Bad request", "details": resp.json()}
        resp.raise_for_status()
        updated = msgspec.convert(resp.json(), type=TandoorMealPlan)
        return msgspec.to_builtins(updated)

    def delete_meal_plan(self, meal_plan_id: int) -> dict[str, Any]:
        """Delete a meal plan entry.

        Args:
            meal_plan_id: The Tandoor meal plan entry ID.
        """
        resp = self.client.delete(f"/api/meal-plan/{meal_plan_id}/")
        if resp.status_code == 404:
            return {"message": f"Meal plan {meal_plan_id} already deleted"}
        resp.raise_for_status()
        return {"message": f"Meal plan {meal_plan_id} deleted successfully"}


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Create a single TandoorClient for the server's lifetime."""
    url = os.environ.get("TANDOOR_URL", "")
    key = os.environ.get("TANDOOR_API_KEY", "")
    timeout = int(os.environ.get("TANDOOR_TIMEOUT", "20"))
    timezone = os.environ.get("TANDOOR_TIMEZONE", "UTC")

    if not url:
        msg = "TANDOOR_URL environment variable is required"
        raise RuntimeError(msg)
    if not key:
        msg = "TANDOOR_API_KEY environment variable is required"
        raise RuntimeError(msg)

    client = TandoorClient(url=url, key=key, timeout=timeout, timezone=timezone)
    try:
        yield {"client": client}
    finally:
        pass


mcp = FastMCP(
    "Tandoor",
    instructions=(
        "MCP server for managing meal plans via Tandoor Recipes. "
        "Use the provided tools to list meal types, search recipes, "
        "and create, read, update, or delete meal plan entries."
    ),
    lifespan=lifespan,
)


def _get_client(ctx: Context) -> TandoorClient:
    """Retrieve the shared TandoorClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


@mcp.tool()
def list_meal_types(ctx: Context) -> list[dict[str, Any]]:
    """List all meal types configured in Tandoor (e.g. breakfast, lunch, dinner).

    Use the returned IDs when creating or updating meal plan entries.
    """
    client = _get_client(ctx)
    return client.list_meal_types()


@mcp.tool()
def search_recipes(
    ctx: Context,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Search recipes in Tandoor by name.

    Args:
        query: Optional search string to filter recipes by name.
    """
    client = _get_client(ctx)
    return client.search_recipes(query=query)


@mcp.tool()
def list_meal_plans(
    ctx: Context,
    from_date: str | None = None,
    to_date: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """List meal plan entries within a date range, optionally filtered by title.

    Args:
        from_date: Start of range as ISO 8601 date (e.g. "2025-01-15"). Defaults to today.
        to_date: End of range as ISO 8601 date. Defaults to 7 days from from_date.
        query: Optional string to filter meal plans by title, note, or recipe name.
    """
    client = _get_client(ctx)
    fd = date.fromisoformat(from_date) if from_date else None
    td = date.fromisoformat(to_date) if to_date else None
    return client.list_meal_plans(from_date=fd, to_date=td, query=query)


@mcp.tool()
def get_meal_plan(ctx: Context, meal_plan_id: int) -> dict[str, Any]:
    """Get full details of a single meal plan entry.

    Args:
        meal_plan_id: The Tandoor meal plan entry ID.
    """
    client = _get_client(ctx)
    result = client.get_meal_plan(meal_plan_id)
    if result is None:
        return {"error": f"Meal plan {meal_plan_id} not found"}
    return result


@mcp.tool()
def create_meal_plan(
    ctx: Context,
    from_date: str,
    to_date: str | None = None,
    meal_type_id: int | None = None,
    title: str = "",
    recipe_id: int | None = None,
    servings: float = 1.0,
    note: str = "",
) -> dict[str, Any]:
    """Create a new meal plan entry in Tandoor.

    Args:
        from_date: Start date as ISO 8601 string (e.g. "2025-01-15").
        to_date: End date as ISO 8601 string. Defaults to from_date.
        meal_type_id: ID of the meal type (use list_meal_types to find IDs).
        title: Title/name for the meal plan entry.
        recipe_id: Optional Tandoor recipe ID to link (use search_recipes to find IDs).
        servings: Number of servings (default 1.0).
        note: Optional note or description.
    """
    client = _get_client(ctx)
    return client.create_meal_plan(
        from_date=from_date,
        to_date=to_date,
        meal_type_id=meal_type_id,
        title=title,
        recipe_id=recipe_id,
        servings=servings,
        note=note,
    )


@mcp.tool()
def update_meal_plan(
    ctx: Context,
    meal_plan_id: int,
    title: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    meal_type_id: int | None = None,
    recipe_id: int | None = None,
    servings: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Update an existing meal plan entry. Only provided fields are changed.

    Args:
        meal_plan_id: The Tandoor meal plan entry ID.
        title: New title.
        from_date: New start date as ISO 8601 string.
        to_date: New end date as ISO 8601 string.
        meal_type_id: New meal type ID.
        recipe_id: New recipe ID to link.
        servings: New number of servings.
        note: New note or description.
    """
    client = _get_client(ctx)
    return client.update_meal_plan(
        meal_plan_id=meal_plan_id,
        title=title,
        from_date=from_date,
        to_date=to_date,
        meal_type_id=meal_type_id,
        recipe_id=recipe_id,
        servings=servings,
        note=note,
    )


@mcp.tool()
def delete_meal_plan(ctx: Context, meal_plan_id: int) -> dict[str, Any]:
    """Delete a meal plan entry from Tandoor.

    Args:
        meal_plan_id: The Tandoor meal plan entry ID.
    """
    client = _get_client(ctx)
    return client.delete_meal_plan(meal_plan_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Tandoor MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
