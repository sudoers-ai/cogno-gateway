"""Unit tests for the EvolutionChannel (WhatsApp) — parse + send with fake httpx."""

import base64

import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    GatewayError,
    MediaRef,
    MessageKind,
    OutboundMessage,
)
from tests.conftest import FakeResponse, body_of

CFG = ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1")


def _ch():
    return EvolutionChannel(CFG)


def _upsert(**data):
    return {"event": "messages.upsert", "data": data}


def test_requires_full_config():
    with pytest.raises(GatewayError):
        EvolutionChannel(ChannelConfig(base_url="x"))


def test_parse_conversation():
    msg = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511@s.whatsapp.net", "fromMe": False, "id": "M1"},
        messageType="conversation", message={"conversation": "oi"}))
    assert msg.kind == MessageKind.TEXT and msg.text == "oi"
    assert msg.sender == "5511@s.whatsapp.net" and msg.message_id == "M1"


def test_parse_extended_text():
    msg = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511@s.whatsapp.net", "id": "M2"},
        messageType="extendedTextMessage",
        message={"extendedTextMessage": {"text": "olá"}}))
    assert msg.text == "olá"


def test_parse_reaction():
    msg = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511@s.whatsapp.net", "id": "M3"},
        message={"reactionMessage": {"text": "🔥", "key": {"id": "TARGET"}}}))
    assert msg.kind == MessageKind.REACTION
    assert msg.reaction.emoji == "🔥" and msg.reaction.target_message_id == "TARGET"


def test_parse_media():
    msg = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511@s.whatsapp.net", "id": "M4"},
        messageType="imageMessage",
        message={"imageMessage": {"caption": "foto", "mimetype": "image/jpeg"}}))
    assert msg.kind == MessageKind.IMAGE and msg.text == "foto"
    assert msg.media.ref == "M4"


def test_ignores_from_me_groups_and_non_upsert():
    ch = _ch()
    assert ch.parse_inbound(_upsert(key={"remoteJid": "x", "fromMe": True})) is None
    assert ch.parse_inbound(_upsert(key={"remoteJid": "123@g.us"})) is None
    assert ch.parse_inbound({"event": "presence.update"}) is None


async def test_send_text_strips_jid_suffix(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "OUT1"}})}
    res = await _ch().send("5511999@s.whatsapp.net", OutboundMessage(text="hi"))
    assert res.ok and res.message_ids == ["OUT1"]
    send = [c for c in fake_httpx.calls if "sendText" in c["url"]][0]
    assert body_of(send)["number"] == "5511999"   # @s.whatsapp.net stripped


async def test_send_audio_base64(fake_httpx):
    fake_httpx.routes = {"sendWhatsAppAudio": FakeResponse({"key": {"id": "A1"}})}
    await _ch().send("5511@s.whatsapp.net", OutboundMessage(audio=b"OPUS"))
    audio = [c for c in fake_httpx.calls if "sendWhatsAppAudio" in c["url"]][0]
    assert base64.b64decode(body_of(audio)["audio"]) == b"OPUS"


async def test_fetch_media_decodes_base64(fake_httpx):
    fake_httpx.routes = {"getBase64": FakeResponse(
        {"base64": base64.b64encode(b"IMG").decode()})}
    data = await _ch().fetch_media(MediaRef(ref="M4"))
    assert data == b"IMG"
