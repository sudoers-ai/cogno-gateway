"""Unit tests for the TelegramChannel (parse + verify + send/fetch with fake httpx)."""

import pytest

from cogno_gateway import (
    ChannelConfig,
    MessageKind,
    OutboundMessage,
    Reaction,
    TelegramChannel,
)
from cogno_gateway.chunker import split_message
from tests.conftest import FakeResponse, body_of

CFG = ChannelConfig(token="BOT123", secret="sek")


def _ch():
    return TelegramChannel(CFG)


def test_requires_token():
    from cogno_gateway import GatewayError
    with pytest.raises(GatewayError):
        TelegramChannel(ChannelConfig())


def test_parse_text_with_reply():
    msg = _ch().parse_inbound({"message": {
        "chat": {"id": 42}, "message_id": 7, "text": "oi",
        "reply_to_message": {"text": "anterior"}}})
    assert msg.kind == MessageKind.TEXT and msg.text == "oi"
    assert msg.sender == "42" and msg.message_id == "7"
    assert msg.reply_to == "anterior"


def test_parse_voice():
    msg = _ch().parse_inbound({"message": {
        "chat": {"id": 1}, "voice": {"file_id": "FID", "mime_type": "audio/ogg"}}})
    assert msg.kind == MessageKind.AUDIO and msg.media.ref == "FID"


def test_parse_photo_and_document():
    photo = _ch().parse_inbound({"message": {
        "chat": {"id": 1}, "photo": [{"file_id": "small"}, {"file_id": "big"}]}})
    assert photo.kind == MessageKind.IMAGE and photo.media.ref == "big"  # largest size
    doc = _ch().parse_inbound({"message": {
        "chat": {"id": 1}, "document": {"file_id": "D", "file_name": "a.pdf"}}})
    assert doc.kind == MessageKind.DOCUMENT and doc.media.filename == "a.pdf"


def test_parse_reaction_and_skips_group():
    msg = _ch().parse_inbound({"message_reaction": {
        "chat": {"id": 5, "type": "private"}, "message_id": 99,
        "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
    assert msg.kind == MessageKind.REACTION and msg.reaction.emoji == "❤"
    assert msg.reaction.target_message_id == "99"
    grp = _ch().parse_inbound({"message_reaction": {
        "chat": {"id": 5, "type": "supergroup"}, "message_id": 99,
        "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
    assert grp is None


def test_non_message_payload_ignored():
    assert _ch().parse_inbound({"edited_message": {}}) is None


def test_parse_drops_group_messages_and_callbacks():
    # 1:1 by design (the WhatsApp @g.us mirror): a group/supergroup message must be dropped in
    # code — BotFather privacy mode is an external toggle, and a delivered @mention would
    # otherwise run a turn keyed by the GROUP chat id (one shared session for all members).
    for chat_type in ("group", "supergroup"):
        assert _ch().parse_inbound({"message": {
            "chat": {"id": -100123, "type": chat_type}, "message_id": 7,
            "text": "@bot marca amanhã"}}) is None
        assert _ch().parse_inbound({"callback_query": {
            "data": "confirm_yes",
            "message": {"chat": {"id": -100123, "type": chat_type}, "message_id": 50}}}) is None
    # a private chat (and the type-less fixtures above) keeps parsing normally
    ok = _ch().parse_inbound({"message": {
        "chat": {"id": 42, "type": "private"}, "message_id": 7, "text": "oi"}})
    assert ok is not None and ok.sender == "42"


def test_parse_callback_query():
    msg = _ch().parse_inbound({"callback_query": {
        "data": "confirm_yes", "message": {"chat": {"id": 7}, "message_id": 50}}})
    assert msg.kind == MessageKind.INTERACTIVE
    assert msg.sender == "7" and msg.selection.id == "confirm_yes"


async def test_send_inline_buttons(fake_httpx):
    from cogno_gateway import Button
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    await _ch().send("42", OutboundMessage(text="Confirma?",
                                           buttons=[Button("yes", "Sim"), Button("no", "Não")]))
    last = [c for c in fake_httpx.calls if "sendMessage" in c["url"]][-1]
    kb = body_of(last)["reply_markup"]["inline_keyboard"]
    assert kb[0][0] == {"text": "Sim", "callback_data": "yes"}


async def test_send_list_as_inline_keyboard(fake_httpx):
    from cogno_gateway import Button, ListMenu, ListSection
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    menu = ListMenu(sections=[ListSection("S", [Button("a", "A"), Button("b", "B"),
                                                Button("c", "C"), Button("d", "D")])])
    await _ch().send("42", OutboundMessage(text="Escolha:", list_menu=menu))
    kb = body_of([c for c in fake_httpx.calls if "sendMessage" in c["url"]][-1])[
        "reply_markup"]["inline_keyboard"]
    assert len(kb) == 4 and kb[3][0]["callback_data"] == "d"


def test_verify():
    ch = _ch()
    assert ch.verify(headers={"x-telegram-bot-api-secret-token": "sek"}, body=b"") is True
    assert ch.verify(headers={"x-telegram-bot-api-secret-token": "x"}, body=b"") is False


def test_verify_open_without_secret_by_default():
    # dev/demo default: no secret configured → open (host guards the route), a warning is logged.
    ch = TelegramChannel(ChannelConfig(token="BOT123"))
    assert ch.verify(headers={}, body=b"") is True


def test_verify_fails_closed_when_secret_required():
    # Security audit 2026-08-04: production sets require_secret=True, so a secretless channel
    # rejects a forged inbound instead of trusting the spoofed sender id.
    ch = TelegramChannel(ChannelConfig(token="BOT123", require_secret=True))
    assert ch.verify(headers={}, body=b"") is False
    # with a secret present, require_secret does not change the normal HMAC path
    ok = TelegramChannel(ChannelConfig(token="BOT123", secret="sek", require_secret=True))
    assert ok.verify(headers={"x-telegram-bot-api-secret-token": "sek"}, body=b"") is True


async def test_send_chunks_text(fake_httpx):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    res = await _ch().send("42", OutboundMessage(text="hi"))
    assert res.ok
    sends = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert sends and body_of(sends[0])["chat_id"] == "42"


async def test_send_reaction(fake_httpx):
    await _ch().send("42", OutboundMessage(reaction=Reaction("👍", "7")))
    react = [c for c in fake_httpx.calls if "setMessageReaction" in c["url"]]
    assert react and body_of(react[0])["reaction"][0]["emoji"] == "👍"


async def test_send_voice_note(fake_httpx):
    fake_httpx.routes = {"sendVoice": FakeResponse({"result": {"message_id": 9}})}
    res = await _ch().send("42", OutboundMessage(audio=b"OPUS", audio_format="ogg"))
    assert res.ok
    voice = [c for c in fake_httpx.calls if "sendVoice" in c["url"]]
    assert voice and "voice" in voice[0]["files"]          # multipart voice upload
    assert voice[0]["data"]["chat_id"] == "42"


async def test_send_document(fake_httpx):
    from cogno_gateway import MediaRef
    await _ch().send("42", OutboundMessage(media=[MediaRef(url="http://x/a.pdf")]))
    docs = [c for c in fake_httpx.calls if "sendDocument" in c["url"]]
    assert docs and body_of(docs[0])["document"] == "http://x/a.pdf"


async def test_send_returns_error_on_http_failure(fake_httpx):
    fake_httpx.routes = {"sendMessage": FakeResponse(status=500)}
    res = await _ch().send("42", OutboundMessage(text="hi"))
    assert res.ok is False and "500" in res.error


@pytest.mark.asyncio
async def test_send_surfaces_telegram_error_description(fake_httpx):
    # a 4xx carries Telegram's reason in the body — surface it (not the opaque httpx status), so
    # "chat not found" / "can't parse entities" is diagnosable instead of a bare "400".
    fake_httpx.routes = {"sendMessage": FakeResponse(
        {"ok": False, "description": "Bad Request: chat not found"}, status=400)}
    res = await _ch().send("42", OutboundMessage(text="hi"))
    assert res.ok is False and "chat not found" in res.error and "400" in res.error


async def test_fetch_media(fake_httpx):
    fake_httpx.routes = {
        "getFile": FakeResponse({"result": {"file_path": "voice/f.ogg"}}),
        "/file/bot": FakeResponse(content=b"AUDIOBYTES"),
    }
    from cogno_gateway import MediaRef
    data = await _ch().fetch_media(MediaRef(ref="FID"))
    assert data == b"AUDIOBYTES"


async def test_send_reaction_failure_is_reported(fake_httpx):
    fake_httpx.routes = {"/setMessageReaction": FakeResponse(status=400)}
    res = await _ch().send("42", OutboundMessage(reaction=Reaction("👍", "7")))
    assert res.ok is False and "400" in res.error


async def test_send_document_failure_is_reported(fake_httpx):
    from cogno_gateway import MediaRef
    fake_httpx.routes = {"/sendDocument": FakeResponse(status=400)}
    res = await _ch().send("42", OutboundMessage(media=[MediaRef(url="http://x/f.pdf")]))
    assert res.ok is False and "400" in res.error


# ──────────────────────────────────────────────────────────────────────────────
# The outbound error path: what the log SAYS, and how many times we try.
#
# Both properties were measured failing in production against a real contact: a reply of
# 1558 chars logged `event=send_failed sent=0 error=` — an empty `error=`, which is
# indistinguishable from no error at all — and no second attempt was ever made, so the reply
# was simply lost. The contact's own message had shown as delivered on their side, so nothing
# prompted them to ask again.
# ──────────────────────────────────────────────────────────────────────────────

class _RecordingAsyncio:
    """Stands in for the module's ``asyncio`` so the backoff is observable and free.

    Patched onto the module's own name rather than onto the real ``asyncio`` module, so a test
    never mutates the loop the test itself is running on."""

    def __init__(self):
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def no_backoff(monkeypatch):
    """Make the retry's pause instantaneous AND recorded."""
    from cogno_gateway import telegram as tg
    clock = _RecordingAsyncio()
    monkeypatch.setattr(tg, "asyncio", clock)
    return clock


def _read_timeout():
    """A read timeout exactly as httpx produces one: httpcore raises ``ReadTimeout(TimeoutError())``,
    httpx re-raises it as ``ReadTimeout(str(inner))``, and ``str(TimeoutError())`` is ``""``. So the
    message is empty in production — this is not a contrived construction."""
    import httpx
    return httpx.ReadTimeout("")


def test_read_timeout_carries_no_message_at_all():
    """The premise of the log defect, pinned separately so it cannot rot silently: if httpx ever
    starts populating this message, the fallback below stops being load-bearing and we should know
    from a failure here rather than by re-reading the source."""
    assert str(_read_timeout()) == ""


async def test_transport_error_is_logged_as_a_class_not_an_empty_string(fake_httpx, no_backoff, caplog):
    """TWIN 1 — a transport error with an empty ``str(exc)`` must name the exception CLASS.

    ``error=`` says nothing; a line that promises to say what happened and says nothing is
    indistinguishable from "there was no error". ``error=ReadTimeout`` is the whole diagnosis
    for this family: the request went out and no answer came back."""
    import logging
    fake_httpx.routes = {"sendMessage": _read_timeout()}   # fails on every attempt
    with caplog.at_level(logging.WARNING, logger="cogno_gateway.telegram"):
        res = await _ch().send("42", OutboundMessage(text="x" * 1558))

    assert res.ok is False
    assert res.error == "ReadTimeout"          # not "" — the mutation this test exists to catch
    failed = [r.getMessage() for r in caplog.records if "event=send_failed" in r.getMessage()]
    assert failed, "the failure must still be logged"
    assert "error=ReadTimeout" in failed[0]
    assert not failed[0].endswith("error="), "an empty error= reads as 'no error happened'"


async def test_transport_failure_is_retried_once_and_the_second_attempt_happens(fake_httpx, no_backoff):
    """TWIN 2 — a TRANSPORT failure gets exactly one retry, and the retry is a real second call.

    The first attempt times out; the second succeeds. The reply that production lost is the reply
    this test recovers."""
    from tests.conftest import Script
    fake_httpx.routes = {"sendMessage": Script(
        _read_timeout(),                                   # attempt 1 — no answer came back
        FakeResponse({"result": {"message_id": 9}}),       # attempt 2 — delivered
    )}
    res = await _ch().send("42", OutboundMessage(text="oi"))

    sends = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert len(sends) == 2, "the retry must be an actual second request, not a re-read"
    assert res.ok is True and res.message_ids == ["9"]
    assert no_backoff.slept, "the retry must WAIT — an instant retry is a second shot at the same broken socket"


async def test_four_hundred_is_not_retried(fake_httpx, no_backoff):
    """TWIN 3 (negative, mandatory) — a 4xx is NOT retried.

    The server answered, and an answer repeated is the same answer: retrying only duplicates the
    request. This is the twin that must FAIL if the retry predicate is ever widened past
    ``httpx.TransportError``."""
    fake_httpx.routes = {"sendMessage": FakeResponse(
        {"ok": False, "description": "Bad Request: chat not found"}, status=400)}
    res = await _ch().send("42", OutboundMessage(text="oi"))

    sends = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert len(sends) == 1, "a 4xx must be attempted exactly once"
    assert no_backoff.slept == [], "no backoff was spent, because no retry was attempted"
    assert res.ok is False and "chat not found" in res.error


async def test_a_send_that_succeeds_first_time_makes_exactly_one_call(fake_httpx, no_backoff):
    """TWIN 4 (over-tightening probe) — the happy path must be untouched: ONE call, not two.

    Denominator: this asserts over the 1 sendMessage call a single-chunk reply produces. A retry
    wired to fire unconditionally would double every message the gateway has ever sent, and the
    three tests above would all still pass."""
    res = await _ch().send("42", OutboundMessage(text="oi"))

    sends = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert len(sends) == 1, f"expected 1 call, saw {len(sends)}: {[c['url'] for c in sends]}"
    assert no_backoff.slept == []
    assert res.ok is True


async def test_the_retry_is_per_call_not_per_message(fake_httpx, no_backoff):
    """The bound that keeps the duplicate to ONE chunk.

    A 1558-char reply is split into several chunks. When a later chunk times out, the chunks that
    already succeeded must NOT be sent again — retrying the whole ``send()`` would re-deliver them
    for certain, trading a maybe-duplicate for a definite one."""
    from tests.conftest import Script
    ok = FakeResponse({"result": {"message_id": 1}})
    fake_httpx.routes = {"sendMessage": Script(ok, _read_timeout(), ok)}
    res = await _ch().send("42", OutboundMessage(text="x " * 800))   # > 600 chars → chunked

    sends = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    chunks = split_message("x " * 800, max_chars=600)
    assert len(chunks) > 1, "the fixture must actually chunk for this test to mean anything"
    # one call per chunk, plus exactly one extra for the single chunk that timed out
    assert len(sends) == len(chunks) + 1
    assert res.ok is True
