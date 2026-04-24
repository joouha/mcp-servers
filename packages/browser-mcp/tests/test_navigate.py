"""Tests for the navigate tool."""

from __future__ import annotations

import pytest

from browser_mcp import ActionResult, BrowserError, navigate


@pytest.mark.asyncio
async def test_navigate_back(ctx, mock_page):
    result = await navigate(ctx, action="back")
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert "back" in result.message
    mock_page.go_back.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigate_forward(ctx, mock_page):
    result = await navigate(ctx, action="forward")
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert "forward" in result.message
    mock_page.go_forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigate_invalid_action(ctx, mock_page):
    result = await navigate(ctx, action="sideways")
    assert isinstance(result, BrowserError)
    assert "sideways" in result.error


@pytest.mark.asyncio
async def test_navigate_error(ctx, mock_page):
    mock_page.go_back.side_effect = Exception("timeout")
    result = await navigate(ctx, action="back")
    assert isinstance(result, BrowserError)
    assert "timeout" in result.error
