"""Read-only smoke tests for the Tandoor MCP server.

Assumes TANDOOR_URL and TANDOOR_API_KEY are set in the environment.

Run with:
    uv run --package tandoor-mcp python packages/tandoor-mcp/tests/test_readonly.py
"""

from __future__ import annotations

import json
import sys

from tandoor_mcp import (
    TandoorClient,
    TandoorMealType,
    TandoorMealPlan,
    TandoorRecipeOverview,
    PaginatedResponse,
    TandoorError,
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
    """Test the TandoorClient directly (not through MCP)."""
    import os

    heading("Direct TandoorClient tests")

    url = os.environ.get("TANDOOR_URL", "")
    key = os.environ.get("TANDOOR_API_KEY", "")
    timezone = os.environ.get("TANDOOR_TIMEZONE", "UTC")

    if not url or not key:
        print("SKIP: TANDOOR_URL/TANDOOR_API_KEY not set")
        return

    client = TandoorClient(url=url, key=key, timezone=timezone)

    # 1. List meal types
    meal_types = client.list_meal_types()
    dump("Meal types", meal_types)
    assert isinstance(meal_types, list)
    for mt in meal_types:
        assert isinstance(mt, TandoorMealType)
        assert mt.name
    print(f"✓ list_meal_types returns list[TandoorMealType] ({len(meal_types)} types)")

    # 2. Search recipes (no query = all)
    recipes = client.search_recipes(page_size=5)
    dump("Recipes (first page, max 5)", recipes)
    assert isinstance(recipes, PaginatedResponse)
    assert isinstance(recipes.count, int)
    for r in recipes.results:
        assert isinstance(r, TandoorRecipeOverview)
        assert r.id
        assert r.name
    print(f"✓ search_recipes returns PaginatedResponse ({recipes.count} total)")

    # 3. Search recipes with query
    search = client.search_recipes(query="chicken", page_size=5)
    dump("Search 'chicken'", search)
    assert isinstance(search, PaginatedResponse)
    for r in search.results:
        assert isinstance(r, TandoorRecipeOverview)
    print(f"✓ search_recipes(query='chicken') returns {search.count} results")

    # 4. List meal plans (default date range)
    plans = client.list_meal_plans()
    dump("Meal plans (next 7 days)", plans)
    assert isinstance(plans, list)
    for p in plans:
        assert isinstance(p, TandoorMealPlan)
    print(f"✓ list_meal_plans returns list[TandoorMealPlan] ({len(plans)} plans)")

    # 5. Get single meal plan (if any exist)
    if plans:
        plan_id = plans[0].id
        plan = client.get_meal_plan(plan_id)
        dump(f"Meal plan detail (id={plan_id})", plan)
        assert plan is not None
        assert isinstance(plan, TandoorMealPlan)
        assert plan.id == plan_id
        print(f"✓ get_meal_plan returns TandoorMealPlan for id={plan_id}")

    # 6. Get non-existent meal plan
    missing = client.get_meal_plan(999999999)
    assert missing is None
    print("✓ get_meal_plan returns None for missing ID")


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
