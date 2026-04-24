"""Tests for the scroll tool."""

from __future__ import annotations

import pytest

from browser_mcp import ActionResult, BrowserError, scroll


@pytest.mark.asyncio
async def test_scroll_down(ctx, mock_page):
    result = await scroll(ctx, direction="down", amount=300)
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert "down" in result.message
    mock_page.mouse.wheel.assert_awaited_once_with(0, 300)


@pytest.mark.asyncio
async def test_scroll_up(ctx, mock_page):
    result = await scroll(ctx, direction="up", amount=200)
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert "up" in result.message
    mock_page.mouse.wheel.assert_awaited_once_with(0, -200)


@pytest.mark.asyncio
async def test_scroll_default(ctx, mock_page):
    result = await scroll(ctx)
    assert isinstance(result, ActionResult)
    mock_page.mouse.wheel.assert_awaited_once_with(0, 500)


@pytest.mark.asyncio
async def test_scroll_error(ctx, mock_page):
    mock_page.mouse.wheel.side_effect = Exception("no page")
    result = await scroll(ctx)
    assert isinstance(result, BrowserError)
    assert "no page" in result.error
