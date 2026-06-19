"""Unit tests for the WhatsAppCloudChannel (Meta Graph API), httpx mocked."""

import hashlib
import hmac

import pytest

from cogno_gateway import (
    ChannelConfig,
    GatewayError,
    MediaRef,
    MessageKind,
    OutboundMessage,
    Reaction,
    Template,
    WhatsAppCloudChannel,
    create_channel,
)
from tests.conftest import FakeResponse, body_of

CFG = ChannelConfig(token="ACCESS", instance="PHONE_ID", secret="appsecret",
                    extra={"verify_token": "vtok"})


def _ch():
    return WhatsAppCloudChannel(CFG)


def _evt(message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def test_requires_token_and_phone_id():
    with pytest.raises(GatewayError):
        WhatsAppCloudChannel(ChannelConfig(token="x"))


def test_parse_text():
    msg = _ch().parse_inbound(_evt({"from": "5511", "id": "wamid.1", "type": "text",
                                    "text": {"body": "oi"}}))
    assert msg.kind == MessageKind.TEXT and msg.text == "oi"
    assert msg.sender == "5511" and msg.message_id == "wamid.1"


def test_parse_reaction():
    msg = _ch().parse_inbound(_evt({"from": "5511", "id": "x", "type": "reaction",
                                    "reaction": {"message_id": "wamid.9", "emoji": "❤️"}}))
    assert msg.kind == MessageKind.REACTION
    assert msg.reaction.emoji == "❤️" and msg.reaction.target_message_id == "wamid.9"


def test_parse_image_and_location():
    img = _ch().parse_inbound(_evt({"from": "5511", "id": "i", "type": "image",
                                    "image": {"id": "MID", "mime_type": "image/jpeg",
                                              "caption": "foto"}}))
    assert img.kind == MessageKind.IMAGE and img.media.ref == "MID" and img.text == "foto"
    loc = _ch().parse_inbound(_evt({"from": "5511", "id": "l", "type": "location",
                                    "location": {"latitude": -23.5, "longitude": -46.6}}))
    assert loc.kind == MessageKind.LOCATION and loc.location.latitude == -23.5


def test_parse_interactive_button_and_list_reply():
    btn = _ch().parse_inbound(_evt({"from": "5511", "id": "x", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Sim"}}}))
    assert btn.kind == MessageKind.INTERACTIVE
    assert btn.selection.id == "yes" and btn.selection.title == "Sim"
    lst = _ch().parse_inbound(_evt({"from": "5511", "id": "x", "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "row1", "title": "Opção A"}}}))
    assert lst.selection.id == "row1"


def test_parse_template_quick_reply_button():
    msg = _ch().parse_inbound(_evt({"from": "5511", "id": "x", "type": "button",
                                    "button": {"payload": "PL", "text": "Confirmar"}}))
    assert msg.kind == MessageKind.INTERACTIVE and msg.selection.id == "PL"


async def test_send_buttons_interactive(fake_httpx):
    from cogno_gateway import Button
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "B1"}]})}
    await _ch().send("5511", OutboundMessage(text="Confirma?", buttons=[Button("yes", "Sim")]))
    b = body_of([c for c in fake_httpx.calls if "/messages" in c["url"]][0])
    assert b["type"] == "interactive"
    assert b["interactive"]["action"]["buttons"][0]["reply"] == {"id": "yes", "title": "Sim"}


def test_statuses_event_ignored():
    assert _ch().parse_inbound({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) is None
    assert _ch().parse_inbound({"object": "x"}) is None


def test_verify_hmac():
    ch = _ch()
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert ch.verify(headers={"x-hub-signature-256": sig}, body=body) is True
    assert ch.verify(headers={"x-hub-signature-256": "sha256=bad"}, body=body) is False


def test_verify_subscription_handshake():
    ch = _ch()
    assert ch.verify_subscription(mode="subscribe", token="vtok", challenge="C123") == "C123"
    assert ch.verify_subscription(mode="subscribe", token="wrong", challenge="C123") is None


async def test_send_text_freeform(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "OUT1"}]})}
    res = await _ch().send("5511", OutboundMessage(text="olá"))
    assert res.ok and res.message_ids == ["OUT1"]
    sent = [c for c in fake_httpx.calls if "/messages" in c["url"]][0]
    b = body_of(sent)
    assert b["type"] == "text" and b["text"]["body"] == "olá" and b["to"] == "5511"


async def test_send_template(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "T1"}]})}
    await _ch().send("5511", OutboundMessage(
        template=Template("appointment_reminder", lang="pt_BR", params=["3ª feira", "14h"])))
    tmpl = [c for c in fake_httpx.calls if "/messages" in c["url"]][0]
    b = body_of(tmpl)
    assert b["type"] == "template"
    assert b["template"]["name"] == "appointment_reminder"
    assert b["template"]["language"]["code"] == "pt_BR"
    assert b["template"]["components"][0]["parameters"][1]["text"] == "14h"


async def test_send_audio_uploads_then_sends(fake_httpx):
    fake_httpx.routes = {
        "/media": FakeResponse({"id": "MEDIA99"}),
        "/messages": FakeResponse({"messages": [{"id": "A1"}]}),
    }
    await _ch().send("5511", OutboundMessage(audio=b"OPUS", audio_format="ogg"))
    upload = [c for c in fake_httpx.calls if c["url"].endswith("/media")][0]
    assert "file" in upload["files"]
    audio_send = [c for c in fake_httpx.calls if "/messages" in c["url"]][0]
    assert body_of(audio_send)["audio"] == {"id": "MEDIA99"}


async def test_send_reaction(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "R1"}]})}
    await _ch().send("5511", OutboundMessage(reaction=Reaction("👍", "wamid.7")))
    react = [c for c in fake_httpx.calls if "/messages" in c["url"]][0]
    b = body_of(react)
    assert b["type"] == "reaction" and b["reaction"]["message_id"] == "wamid.7"


async def test_send_error(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse(status=500)}
    res = await _ch().send("5511", OutboundMessage(text="hi"))
    assert res.ok is False and "500" in res.error


async def test_fetch_media(fake_httpx):
    fake_httpx.routes = {
        "/MID": FakeResponse({"url": "https://lookaside.fb/media/xyz"}),
        "lookaside": FakeResponse(content=b"IMGBYTES"),
    }
    data = await _ch().fetch_media(MediaRef(ref="MID"))
    assert data == b"IMGBYTES"


def test_factory_aliases():
    for kind in ("whatsapp_cloud", "cloud", "meta"):
        assert isinstance(create_channel(kind, CFG), WhatsAppCloudChannel)
