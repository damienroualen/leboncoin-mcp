"""Pure parsing tests on synthetic payloads: no network, no real account data."""

from leboncoin_mcp.guards import is_login_url
from leboncoin_mcp.messaging import (
    parse_conversation,
    parse_history,
    parse_message,
)


def conversation(**overrides):
    raw = {
        "conversation_id": "c1",
        "item": {"id": 42, "subject": "Vélo", "status": "active", "owner_id": "me"},
        "partners": [{"id": "buyer-1", "name": "Camille"}],
        "last_message_sent_at": "2026-11-01T10:00:00Z",
        "last_message_preview": "Bonjour",
        "unseen_counter": 2,
    }
    return raw | overrides


def test_conversation_on_my_ad_is_seller_side():
    parsed = parse_conversation(conversation())
    assert parsed["side"] == "seller"
    assert parsed["ad_id"] == "42"
    assert parsed["partner"] == "Camille"
    assert parsed["unread"] == 2


def test_conversation_on_partner_ad_is_buyer_side():
    raw = conversation(item={"id": 1, "owner_id": "buyer-1"})
    assert parse_conversation(raw)["side"] == "buyer"


def test_conversation_tolerates_missing_fields():
    parsed = parse_conversation({"conversation_id": "c2"})
    assert parsed["partner"] is None
    assert parsed["unread"] == 0


def test_message_authors():
    assert parse_message({"outgoing": True, "type": "ApiMessage"})["author"] == "me"
    assert parse_message({"outgoing": False, "type": "ApiMessage"})["author"] == "partner"
    assert parse_message({"type": "ApiSystemMessage"})["author"] == "system"
    assert parse_message({"type": "ApiIntegrationMessage"})["author"] == "integration"


def test_message_falls_back_to_date_and_counts_attachments():
    parsed = parse_message({"date": "2026-11-01", "attachments": [{}, {}]})
    assert parsed["at"] == "2026-11-01"
    assert parsed["attachments"] == 2


def test_history_without_messages_is_empty():
    assert parse_history({}) == []


def test_history_is_sorted_oldest_first():
    body = {"_embedded": {"messages": [{"id": 2, "sentAt": "2026-11-02"}, {"id": 1, "sentAt": "2026-11-01"}]}}
    assert [m["id"] for m in parse_history(body)] == [1, 2]


def test_login_urls():
    assert is_login_url("https://auth.leboncoin.fr/login/")
    assert not is_login_url("https://www.leboncoin.fr/messages")
