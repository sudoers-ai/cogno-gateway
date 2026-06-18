"""Unit tests for the WebChannel (cogno-cloud-ui JSON contract)."""

from cogno_gateway import MessageKind, OutboundMessage, WebChannel


def test_parse_plain_text():
    ch = WebChannel()
    msg = ch.parse_inbound({"session_id": "s1", "message": "olá"})
    assert msg is not None
    assert msg.channel == "web" and msg.sender == "s1"
    assert msg.kind == MessageKind.TEXT and msg.text == "olá"


def test_parse_empty_ignored():
    ch = WebChannel()
    assert ch.parse_inbound({"session_id": "s1", "message": ""}) is None
    assert ch.parse_inbound({"message": "no session"}) is None


def test_parse_reaction():
    ch = WebChannel()
    msg = ch.parse_inbound({"session_id": "s1",
                            "reaction": {"emoji": "👍", "target_message_id": "m9"}})
    assert msg.kind == MessageKind.REACTION
    assert msg.reaction.emoji == "👍" and msg.reaction.target_message_id == "m9"


def test_parse_media():
    ch = WebChannel()
    msg = ch.parse_inbound({"session_id": "s1", "kind": "image",
                            "media": {"url": "http://x/y.png", "mime": "image/png"}})
    assert msg.kind == MessageKind.IMAGE
    assert msg.media.url == "http://x/y.png" and msg.media.mime == "image/png"


def test_serialize_matches_ui_contract():
    ch = WebChannel()
    out = ch.serialize("s1", OutboundMessage(text="resposta"))
    assert out == {"session_id": "s1", "response": "resposta"}


def test_verify_with_secret():
    ch = WebChannel(secret="shh")
    assert ch.verify(headers={"x-webchat-secret": "shh"}, body=b"") is True
    assert ch.verify(headers={"x-webchat-secret": "nope"}, body=b"") is False
    assert WebChannel().verify(headers={}, body=b"") is True   # open without secret
