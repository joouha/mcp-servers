"""Tests for the screenshot tool."""

from __future__ import annotations

import base64

import pytest

from browser_mcp import BrowserError, ScreenshotResult, screenshot


@pytest.mark.asyncio
async def test_screenshot_success(ctx, mock_page):
    result = await screenshot(ctx)
    assert isinstance(result, ScreenshotResult)
    assert result.url == "https://example.com"
    assert result.title == "Example"
    decoded = base64.b64decode(result.screenshot_b64)
    assert decoded == b"\x89PNG fake"


@pytest.mark.asyncio
async def test_screenshot_error(ctx, mock_page):
    mock_page.screenshot.side_effect = Exception("render failed")
    result = await screenshot(ctx)
    assert isinstance(result, BrowserError)
    assert "render failed" in result.error
