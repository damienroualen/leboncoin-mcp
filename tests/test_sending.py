"""Safety checks around sending, on synthetic data only."""

import json

import pytest

from leboncoin_mcp.config import parse_bool
from leboncoin_mcp.confirm import PendingActions
from leboncoin_mcp.errors import InvalidConfirmation, SendRefused, UnexpectedResponse
from leboncoin_mcp.messaging import (
    MAX_TEXT,
    SendGuard,
    check_conversation_id,
    check_text,
    refuse_duplicate,
    send_refusal,
)

CID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "11111111-2222-4333-8444-555555555555"
SEND_URL = f"https://api.leboncoin.fr/messaging/proxy/api/v1/hal/me-123/conversations/{CID}/messages"


def body(text="Bonjour", attachments=()):
    return json.dumps({"clientMessageId": "x", "text": text, "attachments": list(attachments)}).encode()


# --- the network guard --------------------------------------------------------------------


def test_exact_send_is_allowed():
    assert send_refusal(SEND_URL, body(), CID, "Bonjour") is None


def test_send_to_another_conversation_is_blocked():
    url = SEND_URL.replace(CID, OTHER)
    assert "another conversation" in send_refusal(url, body(), CID, "Bonjour")


@pytest.mark.parametrize(
    "url",
    [
        SEND_URL + "/extra",
        SEND_URL.replace("/messages", "/realtime/typing"),
        f"https://api.leboncoin.fr/x/conversations/{CID}/messages",
    ],
)
def test_other_endpoints_are_blocked(url):
    assert send_refusal(url, body(), CID, "Bonjour")


def test_changed_text_is_blocked():
    assert "differs" in send_refusal(SEND_URL, body("Bonjour !"), CID, "Bonjour")


def test_attachments_and_garbage_are_blocked():
    assert "attachments" in send_refusal(SEND_URL, body(attachments=[{}]), CID, "Bonjour")
    assert "JSON" in send_refusal(SEND_URL, b"text=Bonjour", CID, "Bonjour")
    assert send_refusal(SEND_URL, None, CID, "Bonjour")


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


async def test_guard_passes_reads_and_one_exact_send_only():
    guard = SendGuard(CID, "Bonjour")
    read = FakeRoute(FakeRequest("GET", SEND_URL))
    first = FakeRoute(FakeRequest("POST", SEND_URL, body()))
    second = FakeRoute(FakeRequest("POST", SEND_URL, body()))
    for route in (read, first, second):
        await guard.handle(route)
    assert (read.outcome, first.outcome, second.outcome) == ("passed", "passed", "aborted")
    assert guard.decision.result() is None


async def test_guard_aborts_a_wrong_send_and_reports_why():
    guard = SendGuard(CID, "Bonjour")
    route = FakeRoute(FakeRequest("POST", SEND_URL.replace(CID, OTHER), body()))
    await guard.handle(route)
    assert route.outcome == "aborted"
    assert "another conversation" in guard.decision.result()


# --- inputs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_is_refused(text):
    with pytest.raises(SendRefused):
        check_text(text)


def test_too_long_text_is_refused():
    check_text("a" * MAX_TEXT)
    with pytest.raises(SendRefused):
        check_text("a" * (MAX_TEXT + 1))


@pytest.mark.parametrize("cid", ["", "abc", f"{CID}/../x", CID.upper()])
def test_conversation_id_must_be_a_uuid(cid):
    with pytest.raises(UnexpectedResponse):
        check_conversation_id(cid)


def test_repeating_my_last_message_is_refused():
    history = [{"author": "me", "text": "Bonjour"}, {"author": "partner", "text": "Salut"}]
    refuse_duplicate(history, "Salut")  # their words, not mine
    history.append({"author": "me", "text": "Bonjour Camille"})
    with pytest.raises(SendRefused):
        refuse_duplicate(history, " Bonjour Camille ")
    refuse_duplicate(history, "Bonjour")  # an older message of mine may be said again


# --- confirmation ---------------------------------------------------------------------------


def test_token_works_once():
    store = PendingActions(ttl_s=60)
    token = store.add("send_message", {"text": "Bonjour"})
    assert store.take(token).args == {"text": "Bonjour"}
    with pytest.raises(InvalidConfirmation):
        store.take(token)


def test_token_expires():
    now = [100.0]
    store = PendingActions(ttl_s=60, clock=lambda: now[0])
    token = store.add("send_message", {})
    now[0] += 61
    with pytest.raises(InvalidConfirmation):
        store.take(token)


def test_unknown_token_is_refused():
    with pytest.raises(InvalidConfirmation):
        PendingActions(ttl_s=60).take("made-up")


@pytest.mark.parametrize(
    "value, expected",
    [(None, True), ("", True), ("true", True), ("1", True), ("0", False), ("false", False), ("OFF", False), ("no", False)],
)
def test_confirm_sends_setting(value, expected):
    assert parse_bool(value, True) is expected
