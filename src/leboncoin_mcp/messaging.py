"""Read conversations by watching the real pages load their own data.

Nothing is replayed: a replayed API call once got a DataDome 403 while the page's identical
request succeeded. The cost is that opening a conversation may mark it as read, which is accepted.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from playwright.async_api import Page, Response

from .capture import capture_json
from .config import Settings
from .landlord import tenant_profile_ref

INBOX_URL = "https://www.leboncoin.fr/messages"
CONVERSATION_URL = "https://www.leboncoin.fr/messages/id/{cid}"
CONVERSATIONS_PATH = "/api/messaging-items-api/v3/conversations"
HISTORY_PATH = "/conversations/{cid}/messages"


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


async def get_conversation(
    page: Page, settings: Settings, conversation_id: str
) -> dict[str, Any]:
    cid = quote(conversation_id, safe="")
    path = HISTORY_PATH.format(cid=cid)

    def is_history(response: Response) -> bool:
        return path in response.url and response.request.method == "GET"

    body = await capture_json(
        page, settings, CONVERSATION_URL.format(cid=cid), is_history, "the conversation history"
    )
    messages = parse_history(body)
    return {
        "conversation_id": conversation_id,
        "tenant_profile": latest_tenant_profile(messages),
        "messages": messages,
    }
