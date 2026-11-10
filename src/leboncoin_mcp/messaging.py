"""Read and send messages through the real conversation page.

Reads keep the data the page loads for itself: a replayed API call once got a DataDome 403 while
the page's identical request succeeded. Opening a conversation may mark it as read (accepted).

Sends type into the page's own message box and click its button, like a person. A network
guard in the tab lets the resulting request through only if it targets the requested
conversation with exactly the approved text; anything else is aborted before it leaves Chrome.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, Route

from .capture import capture_json
from .config import Settings
from .errors import NotDelivered, SendRefused, UnexpectedResponse
from .landlord import USER_ID, tenant_profile_ref

INBOX_URL = "https://www.leboncoin.fr/messages"
CONVERSATION_URL = "https://www.leboncoin.fr/messages/id/{cid}"
CONVERSATIONS_PATH = "/api/messaging-items-api/v3/conversations"
HISTORY_PATH = "/conversations/{cid}/messages"
DETAIL_PATH = "/api/messaging-items-api/v3/conversations/{cid}"
SEND_PATH = re.compile(r"^/messaging/proxy/api/v1/hal/[^/]+/conversations/([^/]+)/messages$")
COMPOSER_LABEL = "Ecrire mon message"
SEND_BUTTON_LABEL = "Envoyer mon message"
MAX_TEXT = 5000  # the message box's own maxlength
SEND_TIMEOUT_S = 15.0
UI_TIMEOUT_MS = 10_000


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


def check_text(text: str) -> str:
    if not text.strip():
        raise SendRefused("The message is empty.")
    if len(text) > MAX_TEXT:
        raise SendRefused(f"The message is {len(text)} characters; Leboncoin allows {MAX_TEXT}.")
    return text


def refuse_duplicate(messages: list[dict[str, Any]], text: str) -> None:
    """Refuse to repeat my last message: the usual sign of a retry after a lost answer."""
    mine = [m for m in messages if m["author"] == "me"]
    if mine and mine[-1]["text"].strip() == text.strip():
        raise SendRefused("This is exactly your last message in this conversation; not sent again.")


def send_refusal(url: str, body: bytes | None, conversation_id: str, text: str) -> str | None:
    """Why the page's send request must be blocked, or None if it is exactly the approved one."""
    match = SEND_PATH.match(urlsplit(url).path)
    if not match or match.group(1) != conversation_id:
        return "the page tried to send to another conversation"
    try:
        payload = json.loads(body or b"")
    except ValueError:
        return "the send request was not the expected JSON"
    if not isinstance(payload, dict) or payload.get("text") != text:
        return "the text being sent differs from the approved text"
    if payload.get("attachments"):
        return "the send request carried attachments"
    return None


class SendGuard:
    """Route handler that lets exactly one approved send through and aborts anything else."""

    PATTERN = "**/conversations/*/messages*"

    def __init__(self, conversation_id: str, text: str) -> None:
        self._cid = conversation_id
        self._text = text
        self.decision: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    async def handle(self, route: Route) -> None:
        request = route.request
        if request.method != "POST":
            await route.fallback()
            return
        if self.decision.done():
            refusal: str | None = "a second send request in the same operation"
        else:
            refusal = send_refusal(request.url, request.post_data_buffer, self._cid, self._text)
            self.decision.set_result(refusal)
        if refusal:
            await route.abort()
        else:
            await route.fallback()


async def _type_and_send(page: Page, guard: SendGuard, text: str) -> Response:
    """Type the text, click send, and return Leboncoin's answer to the guarded request."""
    try:
        box = page.get_by_label(COMPOSER_LABEL)
        await box.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        await box.fill(text, timeout=UI_TIMEOUT_MS)
        if await box.input_value(timeout=UI_TIMEOUT_MS) != text:
            raise SendRefused("The message box did not take the exact text; nothing was sent.")
        button = page.get_by_role("button", name=SEND_BUTTON_LABEL, exact=True)
        await button.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        if not await button.is_enabled(timeout=UI_TIMEOUT_MS):
            raise SendRefused("The send button stayed disabled; nothing was sent.")
    except PlaywrightError as exc:
        reason = str(exc).splitlines()[0]
        raise SendRefused(f"Could not prepare the message box ({reason}); nothing was sent.") from None

    def is_send(response: Response) -> bool:
        return response.request.method == "POST" and bool(SEND_PATH.match(urlsplit(response.url).path))

    response_waiter = asyncio.ensure_future(
        page.wait_for_event("response", is_send, timeout=SEND_TIMEOUT_S * 1000)
    )
    try:
        try:
            await button.click(timeout=UI_TIMEOUT_MS)
        except PlaywrightError:
            raise SendRefused("The send button could not be clicked; nothing was sent.") from None
        try:
            refusal = await asyncio.wait_for(asyncio.shield(guard.decision), SEND_TIMEOUT_S)
        except TimeoutError:
            raise NotDelivered("Clicking send produced no send request; nothing was sent.") from None
        if refusal:
            raise SendRefused(f"Blocked before reaching Leboncoin: {refusal}.")
        try:
            return await response_waiter
        except PlaywrightError:
            # The request left Chrome, but its answer did not come back: it may have been sent.
            raise NotDelivered(
                "The send request got no answer; read the conversation before trying again."
            ) from None
    finally:
        response_waiter.cancel()


async def preview_send(
    page: Page, settings: Settings, conversation_id: str, text: str
) -> dict[str, Any]:
    """Everything a person needs to approve a send. Opens the conversation; types nothing."""
    check_text(text)
    view = await open_conversation(page, settings, conversation_id)
    refuse_duplicate(view["messages"], text)
    conversation = view["conversation"]
    return {
        "to": conversation.get("partner"),
        "ad_title": conversation.get("ad_title"),
        "ad_id": conversation.get("ad_id"),
        "conversation_id": conversation_id,
        "recent_messages": view["messages"][-3:],
        "text": text,
    }


async def send_message(
    page: Page, settings: Settings, conversation_id: str, text: str
) -> dict[str, Any]:
    check_text(text)
    view = await open_conversation(page, settings, conversation_id)
    refuse_duplicate(view["messages"], text)

    guard = SendGuard(conversation_id, text)
    await page.route(SendGuard.PATTERN, guard.handle)
    try:
        response = await _type_and_send(page, guard, text)
    finally:
        await page.unroute(SendGuard.PATTERN, guard.handle)

    if response.status not in (200, 201):
        raise NotDelivered(f"Leboncoin answered HTTP {response.status} to the send; check the conversation.")

    # A 201 is not proof: confirm from a fresh read that the message is in the conversation.
    after = await open_conversation(page, settings, conversation_id)
    sent = next(
        (m for m in reversed(after["messages"]) if m["author"] == "me" and m["text"].strip() == text.strip()),
        None,
    )
    if sent is None:
        raise NotDelivered("Leboncoin accepted the send, but a fresh read does not show it yet.")
    return {"sent": True, "to": after["conversation"].get("partner"), "message": sent}
