"""Unit tests for the TelegramChannel (parse + verify + send/fetch with fake httpx)."""

import pytest

from cogno_gateway import (
    ChannelConfig,
    MessageKind,
    OutboundMessage,
    Reaction,
    TelegramChannel,
)
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


def test_verify():
    ch = _ch()
    assert ch.verify(headers={"x-telegram-bot-api-secret-token": "sek"}, body=b"") is True
    assert ch.verify(headers={"x-telegram-bot-api-secret-token": "x"}, body=b"") is False


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


async def test_fetch_media(fake_httpx):
    fake_httpx.routes = {
        "getFile": FakeResponse({"result": {"file_path": "voice/f.ogg"}}),
        "/file/bot": FakeResponse(content=b"AUDIOBYTES"),
    }
    from cogno_gateway import MediaRef
    data = await _ch().fetch_media(MediaRef(ref="FID"))
    assert data == b"AUDIOBYTES"
