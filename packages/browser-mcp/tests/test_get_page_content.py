"""Tests for the get_page_content tool."""

from __future__ import annotations

import pytest

from browser_mcp import BrowserError, PageContent, get_page_content


@pytest.mark.asyncio
async def test_get_page_content_success(ctx, mock_page):
    result = await get_page_content(ctx)
    assert isinstance(result, PageContent)
    assert result.url == "https://example.com"
    assert result.title == "Example"
    assert result.text == "Hello world"


@pytest.mark.asyncio
async def test_get_page_content_error(ctx, mock_page):
    mock_page.title.side_effect = Exception("page crashed")
    result = await get_page_content(ctx)
    assert isinstance(result, BrowserError)
    assert "page crashed" in result.error
