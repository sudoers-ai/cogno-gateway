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


def test_verify_failure_logs_warning(caplog):
    import logging
    ch = WebChannel(secret="shh")
    with caplog.at_level(logging.WARNING, logger="cogno_gateway.web"):
        assert ch.verify(headers={"x-webchat-secret": "nope"}, body=b"") is False
        ch.verify(headers={"x-webchat-secret": "shh"}, body=b"")   # ok → no warning
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "channel=web event=verify_failed" in warnings[0].message


def test_serialize_rich_reply():
    from cogno_gateway import MediaRef, Reaction
    out = WebChannel().serialize("s1", OutboundMessage(
        text="oi", audio=b"x", audio_format="opus",
        media=[MediaRef(url="http://x/y.png", mime="image/png", caption="c")],
        reaction=Reaction("👍", "m1")))
    assert out["media"][0] == {"url": "http://x/y.png", "mime": "image/png", "caption": "c"}
    assert out["audio_format"] == "opus"
    assert out["reaction"] == {"emoji": "👍", "target_message_id": "m1"}


def test_serialize_buttons_and_list():
    from cogno_gateway import Button, ListMenu, ListSection
    btn = WebChannel().serialize("s1", OutboundMessage(text="?", buttons=[Button("a", "A")]))
    assert btn["buttons"] == [{"id": "a", "title": "A"}]
    lst = WebChannel().serialize("s1", OutboundMessage(text="?", list_menu=ListMenu(
        button="Ver", sections=[ListSection("S", [Button("x", "X")])])))
    assert lst["list"]["button"] == "Ver"
    assert lst["list"]["sections"][0]["rows"][0] == {"id": "x", "title": "X"}


async def test_send_is_noop_ok():
    res = await WebChannel().send("s1", OutboundMessage(text="oi"))
    assert res.ok is True


async def test_fetch_media_not_supported():
    import pytest
    from cogno_gateway import MediaRef
    with pytest.raises(NotImplementedError):
        await WebChannel().fetch_media(MediaRef(ref="x"))
