"""Shared fixtures for browser-mcp tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import browser_mcp


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the global BrowserState before each test."""
    browser_mcp._state = browser_mcp.BrowserState()
    yield
    browser_mcp._state = browser_mcp.BrowserState()


def _make_mock_page(
    *,
    url: str = "https://example.com",
    title: str = "Example",
    body_text: str = "Hello world",
) -> AsyncMock:
    """Build a mock Playwright page with sensible defaults."""
    page = AsyncMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.inner_text = AsyncMock(return_value=body_text)
    page.screenshot = AsyncMock(return_value=b"\x89PNG fake")
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.press = AsyncMock()
    page.go_back = AsyncMock()
    page.go_forward = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.mouse = AsyncMock()
    page.mouse.wheel = AsyncMock()
    return page


@pytest_asyncio.fixture
async def mock_page():
    """Patch _state so _page() returns a mock without launching Camoufox."""
    page = _make_mock_page()
    state = browser_mcp._state
    state.page = page
    state._started = True
    return page


@pytest.fixture
def ctx() -> MagicMock:
    """Minimal mock Context for tool calls."""
    return MagicMock()
