"""A network guard: let exactly one approved write leave Chrome, abort anything else.

Writes are done by clicking the page's own buttons, so the page builds the request. The guard
checks that request before it leaves the browser: if the page targets the wrong conversation or
ad, or carries anything other than what was approved, the request is aborted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection

from playwright.async_api import Request, Route


class RequestGuard:
    """Route handler deciding once, for the first request with a guarded method.

    `refusal(request)` returns why the request must be blocked, or None if it is exactly the
    approved one. The decision is published on `decision` so the caller can wait for it.
    """

    def __init__(self, methods: Collection[str], refusal: Callable[[Request], str | None]) -> None:
        self._methods = frozenset(methods)
        self._refusal = refusal
        # A future is a value that will be set later: the caller awaits it after clicking.
        self.decision: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    async def handle(self, route: Route) -> None:
        request = route.request
        if request.method not in self._methods:
            await route.fallback()  # reads go through untouched
            return
        if self.decision.done():
            refusal: str | None = "a second request in the same operation"
        else:
            refusal = self._refusal(request)
            self.decision.set_result(refusal)
        if refusal:
            await route.abort()
        else:
            await route.fallback()
