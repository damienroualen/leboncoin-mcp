"""Two-phase writes: `prepare` stores an action under a token, `confirm` runs it once.

asyncio note: no lock is needed. The event loop runs one coroutine at a time and only switches
at an `await`; `take` has no `await` between finding a token and removing it, so two concurrent
confirmations of the same token cannot both succeed.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import InvalidConfirmation


@dataclass(frozen=True)
class Pending:
    tool: str
    args: dict[str, Any]
    expires_at: float


class PendingActions:
    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._items: dict[str, Pending] = {}

    def add(self, tool: str, args: dict[str, Any]) -> str:
        self._purge()
        token = secrets.token_urlsafe(16)
        self._items[token] = Pending(tool, dict(args), self._clock() + self.ttl_s)
        return token

    def take(self, token: str) -> Pending:
        """Return the action and forget it: a token works once."""
        self._purge()
        pending = self._items.pop(token, None)
        if pending is None:
            raise InvalidConfirmation()
        return pending

    def _purge(self) -> None:
        # Expired entries go on every access, so the store cannot grow without bound.
        now = self._clock()
        for token in [t for t, p in self._items.items() if p.expires_at <= now]:
            del self._items[token]
