"""Tests for the open_page tool."""

from __future__ import annotations

import pytest

from browser_mcp import BrowserError, PageContent, open_page


@pytest.mark.asyncio
async def test_open_page_success(ctx, mock_page):
    mock_page.goto = pytest.importorskip("unittest.mock").AsyncMock(
        side_effect=_update_url(mock_page, "https://example.com/page")
    )

    result = await open_page(ctx, url="https://example.com/page")

    assert isinstance(result, PageContent)
    assert result.url == "https://example.com/page"
    assert result.title == "Example"
    assert result.text == "Hello world"
    mock_page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_page_truncates_long_text(ctx, mock_page):
    mock_page.inner_text.return_value = "x" * 100_000
    result = await open_page(ctx, url="https://example.com")
    assert isinstance(result, PageContent)
    assert len(result.text) == 50_000


@pytest.mark.asyncio
async def test_open_page_error(ctx, mock_page):
    mock_page.goto.side_effect = Exception("net::ERR_NAME_NOT_RESOLVED")
    result = await open_page(ctx, url="https://doesnotexist.invalid")
    assert isinstance(result, BrowserError)
    assert "ERR_NAME_NOT_RESOLVED" in result.error


def _update_url(page, new_url: str):
    async def _side_effect(*args, **kwargs):
        page.url = new_url

    return _side_effect
