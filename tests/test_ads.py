"""Ads: parsing and the delete guard, on synthetic data only."""

import json

import pytest

from leboncoin_mcp.ads import (
    DeleteGuard,
    ad_link_selector,
    check_ad_id,
    check_deletable,
    delete_refusal,
    find_ad,
    parse_ad,
)
from leboncoin_mcp.errors import ActionRefused

AD = "1234567890"
DELETE_URL = "https://api.leboncoin.fr/api/pintad/v1/public/manual/delete/ads"


def body(*ids):
    return json.dumps({"list_ids": [int(i) for i in ids]}).encode()


def test_parse_ad():
    raw = {
        "list_id": 1234567890,
        "subject": "Chambre en colocation",
        "status": "active",
        "category_name": "Locations",
        "price": [350],
        "price_cents": 35000,
        "images": {"nb_images": 4, "thumb_url": "https://img.example/x.jpg"},
    }
    assert parse_ad(raw) == {
        "ad_id": AD,
        "title": "Chambre en colocation",
        "status": "active",
        "category": "Locations",
        "price": 350.0,
        "photos": 4,
    }


def test_parse_ad_without_price_or_photos():
    ad = parse_ad({"list_id": 1, "subject": "Don"})
    assert (ad["price"], ad["photos"]) == (None, 0)


@pytest.mark.parametrize("ad_id", ["", "12a", "1/2", "1 2", "-1", "1" * 16, f'{AD}"]'])
def test_ad_id_must_be_digits(ad_id):
    with pytest.raises(ActionRefused):
        check_ad_id(ad_id)


def test_find_ad_refuses_an_unknown_ad():
    ads = [{"ad_id": "1"}, {"ad_id": AD}]
    assert find_ad(ads, AD) == {"ad_id": AD}
    with pytest.raises(ActionRefused):
        find_ad(ads, "123456789")  # a prefix of AD is another ad


def test_inactive_ads_cannot_be_deleted():
    check_deletable({"ad_id": AD, "status": "paused"})
    with pytest.raises(ActionRefused, match="inactive"):
        check_deletable({"ad_id": AD, "status": "inactive"})


def test_link_selector_matches_only_this_id():
    selector = ad_link_selector(AD)
    assert f'href$="/{AD}"' in selector and f'href*="/{AD}/"' in selector
    assert f'href*="{AD}"' not in selector  # a bare substring would match longer ids


# --- the network guard --------------------------------------------------------------------


def test_exact_delete_is_allowed():
    assert delete_refusal("DELETE", DELETE_URL, body(AD), AD) is None


@pytest.mark.parametrize(
    "payload",
    [
        body("999"),
        body(AD, "999"),
        body(),
        json.dumps({"list_ids": [int(AD)], "all": True}).encode(),
        json.dumps({"list_ids": [AD]}).encode(),  # a string id is not what the page sends
        b"list_ids=1234567890",
        None,
    ],
)
def test_any_other_delete_is_blocked(payload):
    assert delete_refusal("DELETE", DELETE_URL, payload, AD)


def test_other_methods_and_endpoints_are_blocked():
    assert delete_refusal("POST", DELETE_URL, body(AD), AD)
    assert delete_refusal("DELETE", DELETE_URL + "/all", body(AD), AD)


class FakeRequest:
    def __init__(self, method, url, data=None):
        self.method, self.url, self.post_data_buffer = method, url, data


class FakeRoute:
    def __init__(self, request):
        self.request, self.outcome = request, None

    async def abort(self):
        self.outcome = "aborted"

    async def fallback(self):
        self.outcome = "passed"


async def test_guard_passes_reads_and_one_exact_delete_only():
    guard = DeleteGuard(AD)
    read = FakeRoute(FakeRequest("GET", DELETE_URL))
    first = FakeRoute(FakeRequest("DELETE", DELETE_URL, body(AD)))
    second = FakeRoute(FakeRequest("DELETE", DELETE_URL, body(AD)))
    for route in (read, first, second):
        await guard.handle(route)
    assert (read.outcome, first.outcome, second.outcome) == ("passed", "passed", "aborted")
    assert guard.decision.result() is None


async def test_guard_aborts_deleting_another_ad():
    guard = DeleteGuard(AD)
    route = FakeRoute(FakeRequest("DELETE", DELETE_URL, body("999")))
    await guard.handle(route)
    assert route.outcome == "aborted"
    assert "other ads" in guard.decision.result()
