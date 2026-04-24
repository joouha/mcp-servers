"""Tests for the click tool."""

from __future__ import annotations

import pytest

from browser_mcp import ActionResult, BrowserError, click


@pytest.mark.asyncio
async def test_click_success(ctx, mock_page):
    result = await click(ctx, selector="#btn")
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert "#btn" in result.message
    mock_page.click.assert_awaited_once_with("#btn", timeout=5000)
    mock_page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")


@pytest.mark.asyncio
async def test_click_custom_timeout(ctx, mock_page):
    result = await click(ctx, selector=".link", timeout=10000)
    assert isinstance(result, ActionResult)
    mock_page.click.assert_awaited_once_with(".link", timeout=10000)


@pytest.mark.asyncio
async def test_click_error(ctx, mock_page):
    mock_page.click.side_effect = Exception("element not found")
    result = await click(ctx, selector="#missing")
    assert isinstance(result, BrowserError)
    assert "element not found" in result.error
