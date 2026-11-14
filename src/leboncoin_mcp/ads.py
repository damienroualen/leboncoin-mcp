"""My ads, read and deleted through « Mes annonces », like a person.

Reading keeps the ad list the page loads for itself. Deleting clicks the ad's own « Supprimer »
button and the dialog's « Valider », after checking the dialog shows the right ad. A network
guard lets the delete request through only if it names exactly that one ad.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response

from .capture import capture_json
from .config import Settings
from .errors import ActionRefused, ActionUnconfirmed
from .netguard import RequestGuard

DASHBOARD_URL = "https://www.leboncoin.fr/compte/part/mes-annonces"
DASHBOARD_PATH = "/api/dashboard/v1/search"
DELETE_PATH = "/api/pintad/v1/public/manual/delete/ads"
AD_ID = re.compile(r"[0-9]{1,15}")
DELETE_TIMEOUT_S = 15.0
UI_TIMEOUT_MS = 10_000


def parse_ad(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce one dashboard ad to what a client needs."""
    cents = raw.get("price_cents")
    return {
        "ad_id": str(raw.get("list_id") or ""),
        "title": raw.get("subject") or "",
        "status": raw.get("status") or "",  # e.g. active, paused, inactive (being reviewed)
        "category": raw.get("category_name") or "",
        "price": cents / 100 if isinstance(cents, int) else None,
        "photos": int((raw.get("images") or {}).get("nb_images") or 0),
    }


def check_ad_id(ad_id: str) -> str:
    # Ad ids are digits; anything else must not reach a selector or a request.
    if not AD_ID.fullmatch(ad_id):
        raise ActionRefused("ad_id must be the digits from list_ads.")
    return ad_id


def is_dashboard(response: Response) -> bool:
    return response.request.method == "POST" and urlsplit(response.url).path == DASHBOARD_PATH


async def open_dashboard(page: Page, settings: Settings) -> list[dict[str, Any]]:
    body = await capture_json(page, settings, DASHBOARD_URL, is_dashboard, "your ads")
    return [parse_ad(raw) for raw in body.get("ads") or []]


async def list_ads(page: Page, settings: Settings) -> dict[str, Any]:
    ads = await open_dashboard(page, settings)
    # The page asks for 30 ads at a time; a full page means there may be more.
    return {"ads": ads, "has_more": len(ads) >= 30}


def find_ad(ads: list[dict[str, Any]], ad_id: str) -> dict[str, Any]:
    ad = next((a for a in ads if a["ad_id"] == ad_id), None)
    if ad is None:
        raise ActionRefused(f"Ad {ad_id} is not among your ads (see list_ads); nothing was deleted.")
    return ad


def check_deletable(ad: dict[str, Any]) -> dict[str, Any]:
    # « Mes annonces » shows inactive ads (e.g. « En cours de vérification ») without buttons.
    if ad["status"] == "inactive":
        raise ActionRefused(
            f"Ad {ad['ad_id']} is inactive (e.g. being reviewed by Leboncoin), and Leboncoin offers "
            "no delete for it until it is online; nothing was deleted."
        )
    return ad


def delete_refusal(method: str, url: str, body: bytes | None, ad_id: str) -> str | None:
    """Why the page's delete request must be blocked, or None if it deletes exactly that ad."""
    if method != "DELETE" or urlsplit(url).path != DELETE_PATH:
        return "the page sent an unexpected delete request"
    try:
        payload = json.loads(body or b"")
    except ValueError:
        return "the delete request was not the expected JSON"
    if payload != {"list_ids": [int(ad_id)]}:
        return "the delete request names other ads than the approved one"
    return None


class DeleteGuard(RequestGuard):
    PATTERN = "**/manual/delete/**"

    def __init__(self, ad_id: str) -> None:
        def refusal(request: Request) -> str | None:
            return delete_refusal(request.method, request.url, request.post_data_buffer, ad_id)

        super().__init__(("POST", "PUT", "PATCH", "DELETE"), refusal)


def ad_link_selector(ad_id: str) -> str:
    """Visible links to this exact ad: /ad/<category>/<id> or /annonce/<id>/..."""
    return ", ".join(
        f'a[href{op}"{value}"]:visible'
        for op, value in (("$=", f"/{ad_id}"), ("*=", f"/{ad_id}/"), ("*=", f"/{ad_id}?"))
    )


def normalize(text: str) -> str:
    return " ".join(text.split())


async def _open_delete_dialog(page: Page, ad: dict[str, Any]):
    """Open this ad's delete dialog and return its « Valider » button. Deletes nothing."""
    try:
        link = page.locator(ad_link_selector(ad["ad_id"])).first
        await link.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        # The ad's card: the nearest block around its link that holds a « Supprimer » button.
        card = link.locator("xpath=ancestor::div[.//button[@title='Supprimer']][1]")
        delete = card.locator("button[title='Supprimer']:visible")
        if await delete.count() != 1:
            raise ActionRefused("Could not single out this ad's delete button; nothing was deleted.")
        await delete.click(timeout=UI_TIMEOUT_MS)
        dialog = page.get_by_role("dialog")
        await dialog.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        if normalize(ad["title"]) not in normalize(await dialog.inner_text()):
            raise ActionRefused("The delete dialog shows another ad; nothing was deleted.")
        confirm = dialog.get_by_role("button", name="Valider", exact=True)
        await confirm.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        return confirm
    except PlaywrightError as exc:
        reason = str(exc).splitlines()[0]
        raise ActionRefused(f"Could not open the delete dialog ({reason}); nothing was deleted.") from None


async def _confirm_delete(page: Page, guard: DeleteGuard, confirm) -> Response:
    """Click « Valider » and return Leboncoin's answer to the guarded delete request."""

    def is_delete(response: Response) -> bool:
        return response.request.method == "DELETE" and urlsplit(response.url).path == DELETE_PATH

    # Start waiting for the answer *before* clicking, or a fast answer would be missed.
    response_waiter = asyncio.ensure_future(
        page.wait_for_event("response", is_delete, timeout=DELETE_TIMEOUT_S * 1000)
    )
    try:
        try:
            await confirm.click(timeout=UI_TIMEOUT_MS)
        except PlaywrightError:
            raise ActionRefused("« Valider » could not be clicked; nothing was deleted.") from None
        try:
            # shield: if the wait times out, the guard's future itself must not be cancelled.
            refusal = await asyncio.wait_for(asyncio.shield(guard.decision), DELETE_TIMEOUT_S)
        except TimeoutError:
            raise ActionUnconfirmed("« Valider » produced no delete request; check list_ads.") from None
        if refusal:
            raise ActionRefused(f"Blocked before reaching Leboncoin: {refusal}.")
        try:
            return await response_waiter
        except PlaywrightError:
            raise ActionUnconfirmed(
                "The delete request got no answer; check list_ads before trying again."
            ) from None
    finally:
        response_waiter.cancel()


async def preview_delete(page: Page, settings: Settings, ad_id: str) -> dict[str, Any]:
    """The ad a person is about to delete. Opens « Mes annonces »; clicks nothing."""
    check_ad_id(ad_id)
    return check_deletable(find_ad(await open_dashboard(page, settings), ad_id))


async def delete_ad(page: Page, settings: Settings, ad_id: str) -> dict[str, Any]:
    check_ad_id(ad_id)
    ad = check_deletable(find_ad(await open_dashboard(page, settings), ad_id))

    guard = DeleteGuard(ad_id)
    await page.route(DeleteGuard.PATTERN, guard.handle)
    try:
        confirm = await _open_delete_dialog(page, ad)
        response = await _confirm_delete(page, guard, confirm)
    finally:
        await page.unroute(DeleteGuard.PATTERN, guard.handle)

    if response.status not in (200, 204):
        raise ActionUnconfirmed(f"Leboncoin answered HTTP {response.status} to the delete; check list_ads.")

    # Leboncoin accepted exactly this delete. Its ad list catches up within a minute or two, so a
    # still-listed ad is reported, not treated as a failure (which could prompt a retry).
    if any(a["ad_id"] == ad_id for a in await open_dashboard(page, settings)):
        return {
            "deleted": True,
            "verified": False,
            "note": "Leboncoin accepted the delete; the ad usually leaves list_ads within 2 minutes.",
            "ad": ad,
        }
    return {"deleted": True, "verified": True, "ad": ad}
