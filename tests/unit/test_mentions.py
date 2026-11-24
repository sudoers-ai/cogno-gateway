"""Group @-mentions (0.1.17): how a mention APPEARS per channel, and the WhatsApp send.

The host composes a group reply's mentions and puts each one's ``channel.mention_token`` in the
text; the gateway never writes an ``@`` by hand. Only ``EvolutionChannel`` notifies — it adds
``mentioned`` to the ``sendText`` body, and only on the chunk that holds the tokens, never
``mentionsEveryOne``. Every other channel sends the text as-is and logs ``mentions_unsupported``.
No mention's id reaches a log line (the gateway's log-policy mould).
"""

import logging

import pytest

from cogno_gateway import (
    MAX_MENTIONS,
    ChannelConfig,
    EvolutionChannel,
    Mention,
    OutboundMessage,
    TelegramChannel,
    WebChannel,
    WhatsAppCloudChannel,
)
from tests.conftest import FakeResponse, body_of

EVO_CFG = ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1")

# Synthetic ids only — invented phone JIDs/LIDs, never a real number.
ANA = "5511900000001@s.whatsapp.net"
BRUNO = "5511900000002@s.whatsapp.net"
CARLA = "5511900000003@s.whatsapp.net"
DANI = "5511900000004@s.whatsapp.net"
HIDDEN_LID = "19284756301928@lid"


def _evo():
    return EvolutionChannel(EVO_CFG)


def _texts_and_mentioned(fake_httpx):
    """Each sendText call as ``(text, mentioned-or-None)``, in send order."""
    out = []
    for c in fake_httpx.calls:
        if "sendText" in c["url"]:
            b = body_of(c)
            out.append((b["text"], b.get("mentioned")))
    return out


# ── mention_token: the single definition, per channel ────────────────────────

def test_evolution_token_is_at_plus_digits():
    assert _evo().mention_token(ANA, "Ana") == "@5511900000001"
    assert _evo().mention_token("5511900000002", "Bruno") == "@5511900000002"  # bare number


def test_evolution_lid_cannot_be_mentioned_falls_back_to_label():
    # A LID is not mentionable in this Evolution version → the token is the label, no "@".
    assert _evo().mention_token(HIDDEN_LID, "Carla") == "Carla"
    assert _evo().mention_token(HIDDEN_LID, "") == ""


@pytest.mark.parametrize("channel", [
    TelegramChannel(ChannelConfig(token="T")),
    WhatsAppCloudChannel(ChannelConfig(token="A", instance="PID")),
    WebChannel(),
])
def test_other_channels_token_is_the_plain_label(channel):
    assert channel.mention_token(ANA, "Ana") == "Ana"


# ── EvolutionChannel.send: the mentioned array ───────────────────────────────

async def test_send_text_carries_mentioned_and_leaves_text_unchanged(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T1"}})}
    text = f"{_evo().mention_token(ANA, 'Ana')} your slot is confirmed"
    res = await _evo().send(
        "123@g.us", OutboundMessage(text=text, mentions=[Mention(ANA, "Ana")]))
    assert res.ok is True
    sent = _texts_and_mentioned(fake_httpx)
    assert sent == [("@5511900000001 your slot is confirmed", [ANA])]


async def test_mentioned_only_in_the_chunk_that_holds_the_token(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    # max_chars forces two chunks; the token sits in the FIRST, a plain tail in the SECOND.
    cfg = ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1", max_chars=20)
    tok = EvolutionChannel(cfg).mention_token(ANA, "Ana")
    text = f"{tok} hello there friend, this runs long enough to split"
    await EvolutionChannel(cfg).send("123@g.us",
                                     OutboundMessage(text=text, mentions=[Mention(ANA, "Ana")]))
    sent = _texts_and_mentioned(fake_httpx)
    assert len(sent) >= 2
    with_token = [m for (t, m) in sent if tok in t]
    without_token = [m for (t, m) in sent if tok not in t]
    assert with_token == [[ANA]]                 # exactly the chunk with the token carries it
    assert all(m is None for m in without_token)  # and no other chunk does


async def test_more_than_max_mentions_raises_before_any_http(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    four = [Mention(ANA, "Ana"), Mention(BRUNO, "Bruno"),
            Mention(CARLA, "Carla"), Mention(DANI, "Dani")]
    assert len(four) == MAX_MENTIONS + 1
    with pytest.raises(ValueError):
        await _evo().send("123@g.us", OutboundMessage(text="hi @everyone", mentions=four))
    assert [c for c in fake_httpx.calls if "sendText" in c["url"]] == []  # nothing was sent


async def test_mentions_everyone_is_never_sent(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    tok = _evo().mention_token(ANA, "Ana")
    await _evo().send("123@g.us",
                      OutboundMessage(text=f"{tok} and the whole room",
                                      mentions=[Mention(ANA, "Ana")]))
    for c in fake_httpx.calls:
        assert "mentionsEveryOne" not in body_of(c)


async def test_absent_token_leaves_the_id_out_and_warns_without_it(fake_httpx, caplog):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    # The host forgot to place Ana's token in the text → she is not mentioned.
    with caplog.at_level(logging.WARNING, logger="cogno_gateway"):
        await _evo().send("123@g.us",
                          OutboundMessage(text="no token here", mentions=[Mention(ANA, "Ana")]))
    sent = _texts_and_mentioned(fake_httpx)
    assert sent == [("no token here", None)]
    warnings = [r.getMessage() for r in caplog.records if "mention_token_absent" in r.getMessage()]
    assert warnings == ["channel=whatsapp event=mention_token_absent"]
    assert all(ANA not in r.getMessage() and "Ana" not in r.getMessage() for r in caplog.records)


async def test_lid_mention_is_not_sent_and_does_not_warn(fake_httpx, caplog):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    # A LID's token is the label (no "@"); the host composes it, nobody is notified, no warning.
    tok = _evo().mention_token(HIDDEN_LID, "Carla")
    with caplog.at_level(logging.WARNING, logger="cogno_gateway"):
        await _evo().send("123@g.us",
                          OutboundMessage(text=f"{tok} welcome",
                                          mentions=[Mention(HIDDEN_LID, "Carla")]))
    assert _texts_and_mentioned(fake_httpx) == [("Carla welcome", None)]
    assert [r for r in caplog.records if "mention_token_absent" in r.getMessage()] == []


async def test_no_mentions_sends_a_plain_body(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "T"}})}
    await _evo().send("5511@s.whatsapp.net", OutboundMessage(text="plain reply"))
    assert _texts_and_mentioned(fake_httpx) == [("plain reply", None)]
    # the body is byte for byte the pre-0.1.17 shape: no "mentioned" key at all
    assert "mentioned" not in body_of(
        [c for c in fake_httpx.calls if "sendText" in c["url"]][0])


# ── the other channels ignore mentions and log mentions_unsupported ──────────

async def test_telegram_ignores_mentions_and_logs(fake_httpx, caplog):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 7}})}
    with caplog.at_level(logging.WARNING, logger="cogno_gateway"):
        await TelegramChannel(ChannelConfig(token="T")).send(
            "42", OutboundMessage(text="Ana done", mentions=[Mention(ANA, "Ana")]))
    sent = body_of([c for c in fake_httpx.calls if "sendMessage" in c["url"]][0])
    assert sent["text"] == "Ana done" and "mentioned" not in sent
    msgs = [r.getMessage() for r in caplog.records if "mentions_unsupported" in r.getMessage()]
    assert msgs == ["channel=telegram event=mentions_unsupported"]
    assert all(ANA not in r.getMessage() for r in caplog.records)


async def test_cloud_ignores_mentions_and_logs(fake_httpx, caplog):
    fake_httpx.routes = {"messages": FakeResponse({"messages": [{"id": "wamid.X"}]})}
    with caplog.at_level(logging.WARNING, logger="cogno_gateway"):
        await WhatsAppCloudChannel(ChannelConfig(token="A", instance="PID")).send(
            "5511", OutboundMessage(text="Ana done", mentions=[Mention(ANA, "Ana")]))
    sent = body_of([c for c in fake_httpx.calls if "messages" in c["url"]][-1])
    assert sent["text"]["body"] == "Ana done"
    msgs = [r.getMessage() for r in caplog.records if "mentions_unsupported" in r.getMessage()]
    assert msgs == ["channel=whatsapp_cloud event=mentions_unsupported"]
    assert all(ANA not in r.getMessage() for r in caplog.records)
