"""Typed errors. Their messages are shown to the MCP client, so they say what to do next."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class LeboncoinError(ToolError):
    """Base class for every expected failure.

    Being a `ToolError`, its message reaches the client. Any other exception is treated by the
    MCP SDK as a crash and reported without its text.
    """


class CaptchaRequired(LeboncoinError):
    def __init__(self, novnc_url: str) -> None:
        super().__init__(f"CAPTCHA: open {novnc_url} and solve it, then retry.")


class NotAuthenticated(LeboncoinError):
    def __init__(self, novnc_url: str) -> None:
        super().__init__(f"Not logged in: open {novnc_url} and log in, then retry.")


class BlockedByDataDome(LeboncoinError):
    """A 403 on an API call. Never retried: insisting is what escalates DataDome."""

    def __init__(self, what: str, novnc_url: str) -> None:
        super().__init__(
            f"HTTP 403 on {what}. Stop and check the browser at {novnc_url} before retrying."
        )


class BrowserBusy(LeboncoinError):
    def __init__(self) -> None:
        super().__init__("Another browser operation is running; retry shortly.")


class BrowserUnavailable(LeboncoinError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"Cannot reach Chrome: {detail}")


class OperationTimeout(LeboncoinError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"Browser operation exceeded {seconds:g}s and was cancelled.")


class UnexpectedResponse(LeboncoinError):
    """Leboncoin answered, but not with what we know how to read."""

