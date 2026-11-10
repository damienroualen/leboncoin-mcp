"""One Chrome, one operation at a time.

asyncio notes:
- `asyncio.Lock` only makes *other tasks* wait; the event loop stays free meanwhile, so the
  server keeps answering. A thread lock here would freeze every client.
- `@asynccontextmanager` turns a generator into an `async with` block: the code before `yield`
  is the setup, the `finally` is the cleanup, and that cleanup also runs on error or cancellation.
- `asyncio.timeout()` works by cancelling the current task, which raises `CancelledError` at
  whichever `await` is in progress. That is why the old bug (lock released while the browser kept
  working) cannot happen: the work is stopped first, and the lock is released last.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Error, Page, Playwright, async_playwright

from .config import Settings
from .errors import BrowserBusy, BrowserUnavailable, OperationTimeout


async def resolve_cdp_url(url: str) -> str:
    """Replace the hostname with its IP: Chrome refuses CDP requests with a non-IP Host header.

    `socket.gethostbyname` would block the whole event loop during the DNS lookup;
    `loop.getaddrinfo` runs it in a worker thread and lets us `await` the result.
    """
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, parts.port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ip = infos[0][4][0]
    return urlunsplit(parts._replace(netloc=f"{ip}:{parts.port}"))


class BrowserSession:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()

    async def stop(self) -> None:
        # `close()` on a CDP connection only disconnects: Chrome and its tabs stay up.
        if self._browser is not None:
            with contextlib.suppress(Error):
                await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def _connected_browser(self) -> Browser:
        """Return a live connection, reconnecting if Chrome restarted since the last call."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._playwright is None:
            raise BrowserUnavailable("session not started")
        try:
            url = await resolve_cdp_url(self._settings.cdp_url)
            self._browser = await self._playwright.chromium.connect_over_cdp(url)
        except (Error, OSError) as exc:
            raise BrowserUnavailable(str(exc).splitlines()[0]) from exc
        return self._browser

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """Hold the browser, open a fresh tab, and always close it afterwards."""
        try:
            async with asyncio.timeout(self._settings.lock_timeout_s):
                await self._lock.acquire()
        except TimeoutError:
            raise BrowserBusy() from None

        try:
            browser = await self._connected_browser()
            if not browser.contexts:
                raise BrowserUnavailable("no browser context")
            # contexts[0] is the real profile, with its cookies. A new context would be logged out.
            page = await browser.contexts[0].new_page()
            page.set_default_timeout(self._settings.navigation_timeout_ms)
            # Leboncoin's messaging app does not render in a background tab (seen when someone
            # was using another tab in noVNC). Our tab must be the visible one.
            await page.bring_to_front()
            try:
                async with asyncio.timeout(self._settings.operation_timeout_s):
                    yield page
            except TimeoutError:
                raise OperationTimeout(self._settings.operation_timeout_s) from None
            finally:
                # `shield` lets the close finish even if this task is cancelled again meanwhile.
                with contextlib.suppress(Error):
                    await asyncio.shield(page.close())
        finally:
            self._lock.release()
