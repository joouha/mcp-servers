"""Tests for the type_text tool."""

from __future__ import annotations

import pytest

from browser_mcp import ActionResult, BrowserError, type_text


@pytest.mark.asyncio
async def test_type_text_success(ctx, mock_page):
    result = await type_text(ctx, selector="#search", text="hello")
    assert isinstance(result, ActionResult)
    assert result.success is True
    mock_page.fill.assert_awaited_once_with("#search", "hello", timeout=5000)
    mock_page.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_type_text_press_enter(ctx, mock_page):
    result = await type_text(ctx, selector="#search", text="query", press_enter=True)
    assert isinstance(result, ActionResult)
    assert result.success is True
    mock_page.fill.assert_awaited_once_with("#search", "query", timeout=5000)
    mock_page.press.assert_awaited_once_with("#search", "Enter")
    mock_page.wait_for_load_state.assert_awaited_once_with("domcontentloaded")


@pytest.mark.asyncio
async def test_type_text_error(ctx, mock_page):
    mock_page.fill.side_effect = Exception("not an input")
    result = await type_text(ctx, selector="#div", text="oops")
    assert isinstance(result, BrowserError)
    assert "not an input" in result.error
