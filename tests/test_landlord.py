import pytest

from leboncoin_mcp.errors import UnexpectedResponse
from leboncoin_mcp.landlord import get_tenant_profile, parse_rental_profile, tenant_profile_ref
from leboncoin_mcp.messaging import latest_tenant_profile, parse_message

USER = "00000000-1111-2222-3333-444444444444"
LINK = f"https://www.leboncoin.fr/compte/espace-bailleur/123?entryPoint=messaging&userId={USER}"


def test_profile_ref_from_link():
    assert tenant_profile_ref(LINK) == {"ad_id": "123", "user_id": USER}


@pytest.mark.parametrize(
    "url",
    [
        "",
        f"https://evil.example/compte/espace-bailleur/123?userId={USER}",
        f"https://www.leboncoin.fr/compte/autre/123?userId={USER}",
        "https://www.leboncoin.fr/compte/espace-bailleur/123?userId=../../x",
    ],
)
def test_profile_ref_rejects_other_links(url):
    assert tenant_profile_ref(url) is None


def test_system_message_with_button():
    raw = {
        "type": "ApiSystemMessage",
        "text": "Apprenez-en plus",
        "typeAttributes": {"header": "Camille a partagé son profil", "label": "Voir", "link": LINK},
    }
    message = parse_message(raw)
    assert message["text"] == "Camille a partagé son profil\nApprenez-en plus"
    assert message["action"] == {"label": "Voir", "url": LINK}
    assert latest_tenant_profile([message]) == {"ad_id": "123", "user_id": USER}


def test_no_profile_without_button():
    assert "action" not in parse_message({"type": "ApiTextMessage", "typeAttributes": {}})
    assert latest_tenant_profile([parse_message({"text": "Bonjour"})]) is None


def test_parse_rental_profile():
    body = {
        "rental_profile": {"name": "Camille", "income": 2000, "warranty": "parents", "tenants_count": 2},
        "last_update_date": "2026-11-01",
    }
    parsed = parse_rental_profile(body)
    assert parsed["name"] == "Camille"
    assert parsed["guarantor"] == "parents"
    assert parsed["profile_updated_at"] == "2026-11-01"
    assert parsed["email"] is None


@pytest.mark.parametrize("ad_id, user_id", [("12a", USER), ("123", "not-a-uuid"), ("1/2", USER)])
async def test_get_tenant_profile_rejects_bad_ids(ad_id, user_id):
    with pytest.raises(UnexpectedResponse):
        await get_tenant_profile(None, None, ad_id, user_id)
