"""Browser automation MCP server powered by Camoufox."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastmcp import Context, FastMCP
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BrowserError(BaseModel):
    """Returned when a browser operation fails."""

    error: str


class PageContent(BaseModel):
    """Textual snapshot of the current page."""

    url: str
    title: str
    text: str


class ScreenshotResult(BaseModel):
    """Base-64 encoded PNG screenshot."""

    url: str
    title: str
    screenshot_b64: str


class ActionResult(BaseModel):
    """Result of a browser action (click / type / scroll / navigate)."""

    url: str
    title: str
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Lifespan – launch / teardown Camoufox
# ---------------------------------------------------------------------------


@dataclass
class BrowserState:
    """Holds the long-lived browser and page objects."""

    browser: Any = None
    page: Any = None
    _cm: Any = None
    _started: bool = False

    async def ensure_started(self) -> None:
        """Download (if needed) and launch Camoufox on first use."""
        if self._started:
            return
        from camoufox.async_api import AsyncCamoufox

        self._cm = AsyncCamoufox(headless=True)
        self.browser = await self._cm.__aenter__()
        ctx = (
            self.browser.contexts[0]
            if self.browser.contexts
            else await self.browser.new_context()
        )
        self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        self._started = True

    async def shutdown(self) -> None:
        """Shut down the browser if it was started."""
        if self._started:
            await self._cm.__aexit__(None, None, None)
            self._started = False


_state = BrowserState()


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Start Camoufox lazily; tear it down when the server stops."""
    try:
        yield
    finally:
        await _state.shutdown()


mcp = FastMCP(
    "Browser MCP",
    instructions="Browser automation tools powered by Camoufox.",
    lifespan=lifespan,
)


async def _page() -> Any:
    """Return the active Playwright page, launching the browser if needed."""
    await _state.ensure_started()
    return _state.page


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def open_page(ctx: Context, url: str) -> PageContent | BrowserError:
    """Open a URL in the browser and return the page text content."""
    try:
        page = await _page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        title = await page.title()
        text = await page.inner_text("body")
        return PageContent(url=page.url, title=title, text=text[:50_000])
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def get_page_content(ctx: Context) -> PageContent | BrowserError:
    """Return the current page's URL, title, and visible text."""
    try:
        page = await _page()
        title = await page.title()
        text = await page.inner_text("body")
        return PageContent(url=page.url, title=title, text=text[:50_000])
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def screenshot(ctx: Context) -> ScreenshotResult | BrowserError:
    """Take a PNG screenshot of the current page (returned as base-64)."""
    try:
        page = await _page()
        png_bytes = await page.screenshot(type="png", full_page=False)
        return ScreenshotResult(
            url=page.url,
            title=await page.title(),
            screenshot_b64=base64.b64encode(png_bytes).decode(),
        )
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def click(
    ctx: Context,
    selector: str,
    timeout: int = 5000,
) -> ActionResult | BrowserError:
    """Click an element matching the given CSS selector."""
    try:
        page = await _page()
        await page.click(selector, timeout=timeout)
        await page.wait_for_load_state("domcontentloaded")
        return ActionResult(
            url=page.url,
            title=await page.title(),
            success=True,
            message=f"Clicked '{selector}'.",
        )
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def type_text(
    ctx: Context,
    selector: str,
    text: str,
    press_enter: bool = False,
    timeout: int = 5000,
) -> ActionResult | BrowserError:
    """Type text into an element matching the given CSS selector.

    Optionally press Enter afterwards (useful for search boxes).
    """
    try:
        page = await _page()
        await page.fill(selector, text, timeout=timeout)
        if press_enter:
            await page.press(selector, "Enter")
            await page.wait_for_load_state("domcontentloaded")
        return ActionResult(
            url=page.url,
            title=await page.title(),
            success=True,
            message=f"Typed into '{selector}'.",
        )
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def scroll(
    ctx: Context,
    direction: str = "down",
    amount: int = 500,
) -> ActionResult | BrowserError:
    """Scroll the page. *direction* is ``up`` or ``down``; *amount* is pixels."""
    try:
        page = await _page()
        delta = amount if direction == "down" else -amount
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(300)
        return ActionResult(
            url=page.url,
            title=await page.title(),
            success=True,
            message=f"Scrolled {direction} by {amount}px.",
        )
    except Exception as exc:
        return BrowserError(error=str(exc))


@mcp.tool()
async def navigate(
    ctx: Context,
    action: str,
) -> ActionResult | BrowserError:
    """Navigate the browser history. *action* is ``back`` or ``forward``."""
    try:
        page = await _page()
        if action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=15_000)
        else:
            return BrowserError(
                error=f"Unknown action '{action}'. Use 'back' or 'forward'."
            )
        return ActionResult(
            url=page.url,
            title=await page.title(),
            success=True,
            message=f"Navigated {action}.",
        )
    except Exception as exc:
        return BrowserError(error=str(exc))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Browser MCP server."""
    mcp.run()
