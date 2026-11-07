"""Espace bailleur: tenant profiles shared with the landlord.

A profile holds personal data a candidate chose to share with this landlord (contact details,
income, job). It is returned to the client and never logged or stored.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Page, Response

from .capture import capture_json
from .config import Settings
from .errors import UnexpectedResponse

PROFILE_PAGE = "https://www.leboncoin.fr/compte/espace-bailleur/{ad_id}?entryPoint=messaging&userId={user_id}"
PROFILE_PATH = "/api/realestate-rental-profile-api/v3/rental-profile/{user_id}"
AD_ID = re.compile(r"[0-9]{1,20}")
USER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def tenant_profile_ref(url: str) -> dict[str, str] | None:
    """`{ad_id, user_id}` from a « Voir son profil locataire » link, else None."""
    parts = urlsplit(url)
    match = re.fullmatch(r"/compte/espace-bailleur/([0-9]+)/?", parts.path)
    user_id = (parse_qs(parts.query).get("userId") or [""])[0]
    if parts.hostname != "www.leboncoin.fr" or not match or not USER_ID.fullmatch(user_id):
        return None
    return {"ad_id": match.group(1), "user_id": user_id}


def parse_rental_profile(body: dict[str, Any]) -> dict[str, Any]:
    profile = body.get("rental_profile") or {}
    return {
        "name": profile.get("name"),
        "pseudo": profile.get("pseudo"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "income": profile.get("income"),
        "professional_status": profile.get("professional_status"),
        "profession": profile.get("profession"),
        "company": profile.get("company"),
        "guarantor": profile.get("warranty"),
        "tenants_count": profile.get("tenants_count"),
        "moving_in_date": profile.get("moving_in_date"),
        "lease_term": profile.get("lease_term"),
        "profile_updated_at": body.get("last_update_date"),
        "registered_at": body.get("user_registration_date"),
    }


async def get_tenant_profile(
    page: Page, settings: Settings, ad_id: str, user_id: str
) -> dict[str, Any]:
    # Strict ids: they are put in a URL, and must not be able to point anywhere else.
    if not AD_ID.fullmatch(ad_id) or not USER_ID.fullmatch(user_id):
        raise UnexpectedResponse("ad_id must be digits and user_id a UUID, as in tenant_profile.")
    path = PROFILE_PATH.format(user_id=user_id)

    def is_profile(response: Response) -> bool:
        return path in response.url and response.request.method == "GET"

    body = await capture_json(
        page,
        settings,
        PROFILE_PAGE.format(ad_id=ad_id, user_id=user_id),
        is_profile,
        "the tenant profile",
    )
    return {"ad_id": ad_id, "user_id": user_id, **parse_rental_profile(body)}
