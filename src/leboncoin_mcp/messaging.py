"""Read conversations by watching the real pages load their own data.

Nothing is replayed: a replayed API call once got a DataDome 403 while the page's identical
request succeeded. Opening a conversation may mark it as read, which is accepted.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response

from .capture import capture_json
from .config import Settings
from .errors import UnexpectedResponse
from .landlord import USER_ID, tenant_profile_ref

INBOX_URL = "https://www.leboncoin.fr/messages"
CONVERSATION_URL = "https://www.leboncoin.fr/messages/id/{cid}"
CONVERSATIONS_PATH = "/api/messaging-items-api/v3/conversations"
HISTORY_PATH = "/conversations/{cid}/messages"
DETAIL_PATH = "/api/messaging-items-api/v3/conversations/{cid}"


def parse_conversation(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce one raw conversation to what a client needs."""
    item = raw.get("item") or {}
    partners = raw.get("partners") or []
    partner_ids = {str(p.get("id") or p.get("user_id") or "") for p in partners} - {""}
    # We are the buyer when a partner owns the ad: any offer there is our own.
    side = "buyer" if str(item.get("owner_id") or "") in partner_ids else "seller"
    return {
        "conversation_id": raw.get("conversation_id"),
        "ad_id": str(item.get("id") or ""),
        "ad_title": item.get("subject") or "",
        "ad_status": item.get("status") or "",
        "side": side,
        "partner": next((p["name"] for p in partners if p.get("name")), None),
        "last_message_at": raw.get("last_message_sent_at"),
        "preview": raw.get("last_message_preview") or "",
        "unread": int(raw.get("unseen_counter") or 0),
    }


def parse_message(raw: dict[str, Any]) -> dict[str, Any]:
    kind = raw.get("type") or ""
    if kind == "ApiSystemMessage":
        author = "system"
    elif kind == "ApiIntegrationMessage":
        author = "integration"
    else:
        author = "me" if raw.get("outgoing") else "partner"
    attributes = raw.get("typeAttributes") or {}
    message = {
        "id": raw.get("id"),
        "at": raw.get("sentAt") or raw.get("date"),
        "author": author,
        "type": kind,
        "text": "\n".join(t for t in (attributes.get("header"), raw.get("text")) if t),
        "attachments": len(raw.get("attachments") or []),
    }
    if attributes.get("link"):
        # A button in the conversation, e.g. « Voir son profil locataire ».
        message["action"] = {"label": attributes.get("label") or "", "url": attributes["link"]}
    return message


def parse_history(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (body.get("_embedded") or {}).get("messages") or []
    return sorted((parse_message(message) for message in raw), key=lambda m: m["at"] or "")


def is_conversation_list(response: Response) -> bool:
    return CONVERSATIONS_PATH in response.url and response.request.method == "GET"


async def open_inbox(page: Page, settings: Settings) -> dict[str, Any]:
    return await capture_json(
        page, settings, INBOX_URL, is_conversation_list, "the conversation list"
    )


async def list_conversations(
    page: Page, settings: Settings, unread_only: bool = False
) -> dict[str, Any]:
    body = await open_inbox(page, settings)
    conversations = [parse_conversation(raw) for raw in body.get("conversations") or []]
    if unread_only:
        conversations = [c for c in conversations if c["unread"]]
    return {
        "conversations": conversations,
        "has_more": bool((body.get("metadata") or {}).get("next_page_hash")),
    }


def latest_tenant_profile(messages: list[dict[str, Any]]) -> dict[str, str] | None:
    """The most recent tenant profile shared in a conversation, if any."""
    for message in reversed(messages):
        ref = tenant_profile_ref((message.get("action") or {}).get("url", ""))
        if ref:
            return ref
    return None


def check_conversation_id(conversation_id: str) -> str:
    # Conversation ids are UUIDs; anything else must not be put in a URL.
    if not USER_ID.fullmatch(conversation_id):
        raise UnexpectedResponse("conversation_id must be a UUID, as in list_conversations.")
    return conversation_id


DETAILS_GRACE_S = 3.0


async def open_conversation(page: Page, settings: Settings, conversation_id: str) -> dict[str, Any]:
    """Open a conversation and return its history, plus who and which ad when the page says so.

    The history is required, and its URL carries the conversation id, which proves which
    conversation it is. The details (other person, ad) are optional: the page does not always
    fetch them, so they come from the details response if any, else from the conversation list.
    """
    cid = check_conversation_id(conversation_id)
    history_path, detail_path = HISTORY_PATH.format(cid=cid), DETAIL_PATH.format(cid=cid)
    seen: dict[str, Any] = {}
    reads: list[asyncio.Future[None]] = []

    async def keep(name: str, response: Response) -> None:
        try:
            seen[name] = await response.json()
        except PlaywrightError:
            pass

    def on_response(response: Response) -> None:
        # A plain callback cannot await, so reading the body is scheduled as its own task.
        if response.request.method != "GET" or response.status != 200:
            return
        path = urlsplit(response.url).path
        if path == detail_path:
            reads.append(asyncio.ensure_future(keep("detail", response)))
        elif path == CONVERSATIONS_PATH:
            reads.append(asyncio.ensure_future(keep("list", response)))

    def is_history(response: Response) -> bool:
        return response.request.method == "GET" and history_path in response.url

    page.on("response", on_response)
    try:
        history = await capture_json(
            page, settings, CONVERSATION_URL.format(cid=cid), is_history, "the conversation history"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DETAILS_GRACE_S
        while "detail" not in seen and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if reads:
            await asyncio.wait(reads, timeout=DETAILS_GRACE_S)
    finally:
        page.remove_listener("response", on_response)
        for read in reads:
            read.cancel()

    detail = seen.get("detail")
    if detail is not None and detail.get("conversation_id") != cid:
        raise UnexpectedResponse("The page loaded a different conversation than requested.")
    if detail is None:
        listed = (seen.get("list") or {}).get("conversations") or []
        detail = next((c for c in listed if c.get("conversation_id") == cid), None)
    messages = parse_history(history)
    return {
        "conversation": parse_conversation(detail) if detail else {"conversation_id": cid},
        "tenant_profile": latest_tenant_profile(messages),
        "messages": messages,
    }


async def get_conversation(
    page: Page, settings: Settings, conversation_id: str
) -> dict[str, Any]:
    view = await open_conversation(page, settings, conversation_id)
    return {"conversation_id": conversation_id, **view}
