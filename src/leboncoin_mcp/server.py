"""MCP tools. Their docstrings are what the client's model reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import messaging
from .browser import BrowserSession
from .config import Settings
from .errors import LeboncoinError
from .guards import check_page

settings = Settings.from_env()
session = BrowserSession(settings)


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    """Start and stop Playwright on the event loop the server owns."""
    await session.start()
    try:
        yield
    finally:
        await session.stop()


mcp = MCPServer("leboncoin", lifespan=lifespan)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def session_status() -> dict[str, Any]:
    """Check that the Leboncoin session works. `logged_in` is false with a `reason` to act on."""
    try:
        async with session.page() as page:
            await messaging.open_inbox(page, settings)
            await check_page(page, settings.novnc_url)
    except LeboncoinError as exc:
        return {"logged_in": False, "reason": str(exc)}
    return {"logged_in": True}


@mcp.tool(annotations=READ_ONLY)
async def list_conversations(unread_only: bool = False) -> dict[str, Any]:
    """List the 50 most recent conversations, without opening them or marking them read.

    Each has `side`: "seller" (a buyer writes about my ad) or "buyer" (I write about someone
    else's ad). `has_more` is true when older conversations exist beyond this page.
    """
    async with session.page() as page:
        return await messaging.list_conversations(page, settings, unread_only)


@mcp.tool(annotations=READ_ONLY)
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    """Read one conversation's history, oldest first. This may mark it as read.

    Message `author` is "me", "partner", "system" (Leboncoin prompts) or "integration"
    (offers and transaction events).
    """
    async with session.page() as page:
        return await messaging.get_conversation(page, settings, conversation_id)


def main() -> None:
    if settings.transport == "streamable-http":
        mcp.run("streamable-http", host=settings.host, port=settings.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
