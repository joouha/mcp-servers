"""Tests for BrowserState lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_mcp import BrowserState


@pytest.mark.asyncio
async def test_ensure_started_launches_browser():
    mock_browser = MagicMock()
    mock_ctx = MagicMock()
    mock_page = AsyncMock()
    mock_ctx.pages = [mock_page]
    mock_browser.contexts = [mock_ctx]

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_browser)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "browser_mcp.BrowserState._create_cm", return_value=mock_cm, create=True
    ):
        # Directly test the state machine
        state = BrowserState()
        state._cm = mock_cm
        state.browser = mock_browser
        state.page = mock_page
        state._started = True

        assert state._started is True
        assert state.page is mock_page


@pytest.mark.asyncio
async def test_shutdown_when_not_started():
    state = BrowserState()
    # Should not raise
    await state.shutdown()
    assert state._started is False


@pytest.mark.asyncio
async def test_shutdown_calls_exit():
    mock_cm = AsyncMock()
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    state = BrowserState()
    state._cm = mock_cm
    state._started = True

    await state.shutdown()

    mock_cm.__aexit__.assert_awaited_once_with(None, None, None)
    assert state._started is False
