"""Tandoor MCP Server.

An MCP server for managing meal plans via the Tandoor Recipes API.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, Field
from fastmcp import Context, FastMCP

log = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Tandoor data structures
# ---------------------------------------------------------------------------


class TandoorUser(BaseModel):
    """A Tandoor user reference."""

    id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    display_name: str = ""


class TandoorKeywordLabel(BaseModel):
    """Keyword as returned in recipe overview (label only)."""

    id: int
    label: str = ""


class TandoorMealType(BaseModel):
    """A meal type (e.g. breakfast, lunch, dinner).

    Matches the ``MealType`` schema from the Tandoor OpenAPI spec.
    """

    id: int | None = None
    name: str = ""
    order: int = 0
    time: str | None = None
    color: str | None = None
    default: bool = False
    created_by: int | None = None


class TandoorRecipeOverview(BaseModel):
    """Recipe overview as embedded in meal plan entries.

    Matches the ``RecipeOverview`` schema from the Tandoor OpenAPI spec.
    """

    id: int
    name: str = ""
    description: str | None = None
    image: str | None = None
    keywords: list[TandoorKeywordLabel] = Field(default_factory=list)
    working_time: int = 0
    waiting_time: int = 0
    created_by: TandoorUser | None = None
    created_at: str | None = None
    updated_at: str | None = None
    internal: bool = True
    servings: int = 1
    servings_text: str = ""
    rating: float | None = None
    last_cooked: str | None = None


class TandoorMealPlan(BaseModel):
    """A meal plan entry.

    Matches the ``MealPlan`` schema from the Tandoor OpenAPI spec.
    """

    id: int | None = None
    title: str = ""
    recipe: TandoorRecipeOverview | None = None
    servings: float = 1.0
    note: str = ""
    note_markdown: str = ""
    from_date: str = ""
    to_date: str = ""
    meal_type: TandoorMealType | None = None
    created_by: int | None = None
    shared: list[TandoorUser] | None = None
    recipe_name: str = ""
    meal_type_name: str = ""
    shopping: bool = False


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper matching Tandoor's pagination."""

    count: int = 0
    next: str | None = None
    previous: str | None = None
    results: list[T] = Field(default_factory=list)


class TandoorError(BaseModel):
    """Structured error response returned by tool functions."""

    error: str
    details: Any = None


class TandoorDeleteResponse(BaseModel):
    """Response returned after a successful delete."""

    message: str


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

    def list_meal_types(self) -> list[TandoorMealType]:
        """List all meal types configured in Tandoor."""
        resp = self.client.get("/api/meal-type/")
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("results", data) if isinstance(data, dict) else data
        return [TandoorMealType.model_validate(mt) for mt in raw]

    # -- recipes ------------------------------------------------------------

    def search_recipes(
        self,
        query: str | None = None,
        keywords: list[int] | None = None,
        foods: list[int] | None = None,
        rating: int | None = None,
        internal: bool | None = None,
        random: bool = False,
        page: int = 1,
        page_size: int = 25,
    ) -> PaginatedResponse[TandoorRecipeOverview]:
        """Search recipes with optional filters.

        Args:
            query: Optional search string to filter recipes by name.
            keywords: Optional list of keyword IDs to filter by (OR logic).
            foods: Optional list of food IDs to filter by (OR logic).
            rating: Optional minimum rating to filter by.
            internal: If True, only return internal recipes.
            random: If True, return results in random order.
            page: Page number for pagination (default 1).
            page_size: Number of results per page (default 25).
        """
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if query:
            params["query"] = query
        if keywords:
            params["keywords"] = keywords
        if foods:
            params["foods"] = foods
        if rating is not None:
            params["rating_gte"] = rating
        if internal is not None:
            params["internal"] = internal
        if random:
            params["random"] = True
        resp = self.client.get("/api/recipe/", params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "results" in data:
            recipes = [TandoorRecipeOverview.model_validate(r) for r in data["results"]]
            return PaginatedResponse[TandoorRecipeOverview](
                count=data.get("count", 0),
                next=data.get("next"),
                previous=data.get("previous"),
                results=recipes,
            )
        recipes = [TandoorRecipeOverview.model_validate(r) for r in data]
        return PaginatedResponse[TandoorRecipeOverview](count=len(recipes), results=recipes)

    # -- recipe import from URL ---------------------------------------------

    def import_recipe_from_url(self, url: str) -> TandoorRecipeOverview | TandoorError:
        """Import a recipe from a URL into Tandoor.

        This is a two-step process:
        1. POST to /api/recipe-from-source/ to scrape/parse the recipe.
        2. POST to /api/recipe/ to create the recipe in Tandoor.

        Args:
            url: The URL of the recipe to import.
        """
        # Step 1: Scrape the recipe from the URL
        resp = self.client.post(
            "/api/recipe-from-source/",
            json={"url": url},
        )
        if resp.status_code == 400:
            return TandoorError(error="Bad request", details=resp.json())
        resp.raise_for_status()
        source_data = resp.json()

        if source_data.get("error"):
            return TandoorError(
                error="Failed to parse recipe from URL",
                details=source_data.get("msg", ""),
            )

        # If the recipe was already created by the scraper (recipe_id present)
        recipe_id = source_data.get("recipe_id")
        if recipe_id:
            get_resp = self.client.get(f"/api/recipe/{recipe_id}/")
            if get_resp.status_code == 200:
                return TandoorRecipeOverview.model_validate(get_resp.json())

        # Step 2: Build the recipe payload from the scraped data
        recipe_data = source_data.get("recipe", {})
        if not recipe_data:
            return TandoorError(error="No recipe data returned from source")

        payload: dict[str, Any] = {
            "name": recipe_data.get("name", "Imported Recipe"),
            "internal": recipe_data.get("internal", True),
            "source_url": recipe_data.get("source_url", url),
        }

        if recipe_data.get("description"):
            payload["description"] = recipe_data["description"]
        if recipe_data.get("servings"):
            payload["servings"] = recipe_data["servings"]
        if recipe_data.get("servings_text"):
            payload["servings_text"] = recipe_data["servings_text"]
        if recipe_data.get("working_time"):
            payload["working_time"] = recipe_data["working_time"]
        if recipe_data.get("waiting_time"):
            payload["waiting_time"] = recipe_data["waiting_time"]

        if recipe_data.get("steps"):
            payload["steps"] = recipe_data["steps"]

        if recipe_data.get("keywords"):
            payload["keywords"] = [
                kw for kw in recipe_data["keywords"]
                if kw.get("import_keyword", True)
            ]

        if recipe_data.get("properties"):
            payload["properties"] = recipe_data["properties"]

        create_resp = self.client.post("/api/recipe/", json=payload)
        if create_resp.status_code == 400:
            return TandoorError(error="Bad request creating recipe", details=create_resp.json())
        create_resp.raise_for_status()
        created = create_resp.json()

        # Step 3: If there's an image URL, upload it
        images = source_data.get("images", [])
        image_url = recipe_data.get("image_url")
        if not image_url and images:
            image_url = images[0]

        if image_url and created.get("id"):
            try:
                self.client.put(
                    f"/api/recipe/{created['id']}/image/",
                    json={"image_url": image_url},
                )
            except Exception:
                log.warning("Failed to upload image for recipe %s", created.get("id"))

        return TandoorRecipeOverview.model_validate(created)

    # -- meal plans ---------------------------------------------------------

    def list_meal_plans(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        query: str | None = None,
    ) -> list[TandoorMealPlan]:
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
        raw = data.get("results", data) if isinstance(data, dict) else data
        plans = [TandoorMealPlan.model_validate(p) for p in raw]
        if query:
            q = query.lower()
            plans = [
                p for p in plans
                if q in p.title.lower()
                or q in p.note.lower()
                or (p.recipe is not None and q in p.recipe.name.lower())
            ]
        return plans

    def get_meal_plan(self, meal_plan_id: int) -> TandoorMealPlan | None:
        """Get a single meal plan entry by ID.

        Args:
            meal_plan_id: The Tandoor meal plan entry ID.
        """
        resp = self.client.get(f"/api/meal-plan/{meal_plan_id}/")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return TandoorMealPlan.model_validate(resp.json())

    def create_meal_plan(
        self,
        from_date: str,
        to_date: str | None = None,
        meal_type_id: int | None = None,
        title: str = "",
        recipe_id: int | None = None,
        servings: float = 1.0,
        note: str = "",
    ) -> TandoorMealPlan | TandoorError:
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
            return TandoorError(error="Bad request", details=resp.json())
        resp.raise_for_status()
        return TandoorMealPlan.model_validate(resp.json())

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
    ) -> TandoorMealPlan | TandoorError:
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
            return TandoorError(error=f"Meal plan {meal_plan_id} not found")
        if resp.status_code == 400:
            return TandoorError(error="Bad request", details=resp.json())
        resp.raise_for_status()
        return TandoorMealPlan.model_validate(resp.json())

    def delete_meal_plan(self, meal_plan_id: int) -> TandoorDeleteResponse:
        """Delete a meal plan entry.

        Args:
            meal_plan_id: The Tandoor meal plan entry ID.
        """
        resp = self.client.delete(f"/api/meal-plan/{meal_plan_id}/")
        if resp.status_code == 404:
            return TandoorDeleteResponse(message=f"Meal plan {meal_plan_id} already deleted")
        resp.raise_for_status()
        return TandoorDeleteResponse(message=f"Meal plan {meal_plan_id} deleted successfully")


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
        "MCP server for managing meal plans and recipes via Tandoor Recipes. "
        "Use the provided tools to list meal types, search recipes, "
        "import recipes from URLs, and create, read, update, or delete "
        "meal plan entries."
    ),
    lifespan=lifespan,
)


def _get_client(ctx: Context) -> TandoorClient:
    """Retrieve the shared TandoorClient from the lifespan context."""
    return ctx.request_context.lifespan_context["client"]


@mcp.tool()
def list_meal_types(ctx: Context) -> list[TandoorMealType]:  # noqa: D103
    """List all meal types configured in Tandoor (e.g. breakfast, lunch, dinner).

    Use the returned IDs when creating or updating meal plan entries.
    """
    client = _get_client(ctx)
    return client.list_meal_types()


@mcp.tool()
def search_recipes(
    ctx: Context,
    query: str | None = None,
    keywords: list[int] | None = None,
    foods: list[int] | None = None,
    rating: int | None = None,
    internal: bool | None = None,
    random: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> PaginatedResponse[TandoorRecipeOverview]:
    """Search recipes in Tandoor with optional filters.

    Args:
        query: Optional search string to filter recipes by name.
        keywords: Optional list of keyword IDs to filter by (OR logic).
        foods: Optional list of food IDs to filter by (OR logic).
        rating: Optional minimum rating to filter by.
        internal: If True, only return internal (non-external) recipes.
        random: If True, return results in random order.
        page: Page number for pagination (default 1).
        page_size: Number of results per page (default 25, max varies by server).
    """
    client = _get_client(ctx)
    return client.search_recipes(
        query=query,
        keywords=keywords,
        foods=foods,
        rating=rating,
        internal=internal,
        random=random,
        page=page,
        page_size=page_size,
    )


@mcp.tool()
def import_recipe_from_url(ctx: Context, url: str) -> TandoorRecipeOverview | TandoorError:
    """Import a recipe from a URL into Tandoor.

    Scrapes the recipe from the given URL using Tandoor's built-in parser,
    creates it as a new recipe, and downloads the recipe image if available.

    Args:
        url: The URL of the recipe page to import (e.g. from a food blog).
    """
    client = _get_client(ctx)
    return client.import_recipe_from_url(url)


@mcp.tool()
def list_meal_plans(
    ctx: Context,
    from_date: str | None = None,
    to_date: str | None = None,
    query: str | None = None,
) -> list[TandoorMealPlan]:
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
def get_meal_plan(ctx: Context, meal_plan_id: int) -> TandoorMealPlan | TandoorError:
    """Get full details of a single meal plan entry.

    Args:
        meal_plan_id: The Tandoor meal plan entry ID.
    """
    client = _get_client(ctx)
    result = client.get_meal_plan(meal_plan_id)
    if result is None:
        return TandoorError(error=f"Meal plan {meal_plan_id} not found")
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
) -> TandoorMealPlan | TandoorError:
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
) -> TandoorMealPlan | TandoorError:
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
def delete_meal_plan(ctx: Context, meal_plan_id: int) -> TandoorDeleteResponse:
    """Delete a meal plan entry from Tandoor.

    Args:
        meal_plan_id: The Tandoor meal plan entry ID.
    """
    client = _get_client(ctx)
    return client.delete_meal_plan(meal_plan_id)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("tandoor://meal-types")
def meal_types_resource(ctx: Context) -> list[TandoorMealType]:
    """All meal types configured in Tandoor (e.g. breakfast, lunch, dinner).

    Use this reference data to look up valid meal type IDs when creating or
    updating meal plan entries.
    """
    client = _get_client(ctx)
    return client.list_meal_types()


@mcp.resource("tandoor://recipe/{recipe_id}")
def recipe_resource(ctx: Context, recipe_id: int) -> TandoorRecipeOverview | TandoorError:
    """Full overview of a single recipe by ID.

    Attach this as context when planning meals around a specific recipe.
    """
    client = _get_client(ctx)
    resp = client.client.get(f"/api/recipe/{recipe_id}/")
    if resp.status_code == 404:
        return TandoorError(error=f"Recipe {recipe_id} not found")
    resp.raise_for_status()
    return TandoorRecipeOverview.model_validate(resp.json())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Tandoor MCP server."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
