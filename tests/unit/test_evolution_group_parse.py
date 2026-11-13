"""Evolution group parse (the author and the quoted id), behind ``accept_groups``.

The shapes here are COPIED from a real capture of four WhatsApp group messages (plain, mention,
reply, reply+mention): the keys, the nesting, the types and the relations between ids are the real
ones; every id, number, name and text is synthetic. What the capture proved, and what these twins
pin:

* in a group the author is NOT in ``key.participant`` — that is always ``<lid>@lid`` — but in the
  bridge ``key.participantAlt = <phone>@s.whatsapp.net``;
* the reply/mention blocks hang off a TOP-LEVEL ``data.contextInfo`` (sibling of ``message``), null
  on a plain message, independent of ``messageType`` (which is ``conversation`` on all four).

The adapter stays OFF by default: with ``accept_groups`` unset every one of the four parses to
``None``, exactly as a group did before this change. Turned on, it resolves the author only from a
phone JID (invariant a), always carries the room as ``conversation_id`` (invariant b), and exposes
the quoted id for the host to decide ``reply_to_bot`` — without ever setting ``mentions_bot``, which
needs the bot's own LID this library does not yet know.
"""

from __future__ import annotations

import dataclasses

import pytest

from cogno_gateway import ChannelConfig, EvolutionChannel, InboundMessage, MessageKind

ROOM = "120363000000000001@g.us"
AUTHOR_LID = "100000000000001@lid"
AUTHOR_PHONE = "5511900000001@s.whatsapp.net"
OTHER_LID = "200000000000002@lid"


def _cfg(accept_groups: bool) -> ChannelConfig:
    return ChannelConfig(base_url="http://evo.invalid:8080/", token="APIKEY", instance="inst-x",
                         accept_groups=accept_groups)


def _upsert(record: dict) -> dict:
    return {"event": "messages.upsert", "data": record}


def _key(msg_id: str, **over) -> dict:
    k = {"id": msg_id, "fromMe": False, "remoteJid": ROOM, "participant": AUTHOR_LID,
         "addressingMode": "lid", "participantAlt": AUTHOR_PHONE}
    k.update(over)
    return k


# ── the four REAL shapes (synthetic values), each wrapped as the webhook record ────────────────
MSG_PLAIN = _upsert({
    "key": _key("3EB00000000000000000000000000001"),
    "pushName": "Membro Teste", "participant": None, "messageType": "conversation",
    "message": {"conversation": "Mensagem simples no grupo.",
                "messageContextInfo": {"threadId": [], "messageSecret": "<opaque:44>"}},
    "contextInfo": None})
MSG_MENTION = _upsert({
    "key": _key("3EB00000000000000000000000000002"),
    "pushName": "Membro Teste", "participant": None, "messageType": "conversation",
    "message": {"conversation": "@200000000000002 pode me ajudar com uma coisa?",
                "messageContextInfo": {"threadId": [], "messageSecret": "<opaque:44>"}},
    "contextInfo": {"mentionedJid": [OTHER_LID], "groupMentions": [], "statusAttributions": []}})
MSG_REPLY = _upsert({
    "key": _key("3EB00000000000000000000000000003"),
    "pushName": "Membro Teste", "participant": None, "messageType": "conversation",
    "message": {"conversation": "Resposta a uma mensagem.",
                "messageContextInfo": {"threadId": [], "messageSecret": "<opaque:44>"}},
    "contextInfo": {"stanzaId": "3EB00000000000000000000000000004", "quotedType": 0,
                    "participant": AUTHOR_LID, "mentionedJid": [], "groupMentions": [],
                    "quotedMessage": {"conversation": "Mensagem anterior do próprio autor."},
                    "statusAttributions": []}})
MSG_REPLY_MENTION = _upsert({
    "key": _key("3EB00000000000000000000000000005"),
    "pushName": "Membro Teste", "participant": None, "messageType": "conversation",
    "message": {"conversation": "@200000000000002 respondendo com menção.",
                "messageContextInfo": {"threadId": [], "messageSecret": "<opaque:44>"}},
    "contextInfo": {"stanzaId": "3EB00000000000000000000000000002", "quotedType": 0,
                    "participant": AUTHOR_LID, "mentionedJid": [OTHER_LID], "groupMentions": [],
                    "quotedMessage": {"conversation": "@200000000000002 pode me ajudar com uma coisa?"},
                    "statusAttributions": []}})

REAL_FOUR = {"plain": MSG_PLAIN, "mention": MSG_MENTION, "reply": MSG_REPLY,
             "reply_mention": MSG_REPLY_MENTION}


# ── OFF by default: the four real messages drop, byte for byte as a group did before ────────────
@pytest.mark.parametrize("label", sorted(REAL_FOUR))
def test_off_by_default_every_real_group_message_drops(label):
    assert EvolutionChannel(_cfg(False)).parse_inbound(REAL_FOUR[label]) is None
    # control: the default ChannelConfig (no accept_groups passed at all) drops it too
    default = EvolutionChannel(ChannelConfig(base_url="http://e:8/", token="K", instance="i"))
    assert default.parse_inbound(REAL_FOUR[label]) is None


def test_off_a_1to1_message_still_parses_beside_the_dropped_group():
    """The switch drops the group, not the channel: a 1:1 message parses with the switch off."""
    one_to_one = _upsert({"key": {"remoteJid": AUTHOR_PHONE, "id": "ONE1", "fromMe": False},
                          "messageType": "conversation", "message": {"conversation": "oi"}})
    msg = EvolutionChannel(_cfg(False)).parse_inbound(one_to_one)
    assert isinstance(msg, InboundMessage) and msg.conversation_id == "" and msg.author == ""


# ── ON: invariant (a) — the author only from a PHONE JID ───────────────────────────────────────
def test_on_the_author_is_the_phone_bridge_not_the_lid():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_PLAIN)
    assert isinstance(msg, InboundMessage)
    assert msg.author == AUTHOR_PHONE            # participantAlt, never key.participant (the @lid)
    assert AUTHOR_LID not in (msg.author, msg.conversation_id)
    assert msg.kind == MessageKind.TEXT and msg.text == "Mensagem simples no grupo."
    assert msg.author_is_bot is False and msg.mentions_bot is False and msg.reply_to_bot is False


@pytest.mark.parametrize("participant_alt", ["200000000000002@lid", "", "5511900000001@g.us",
                                             "5511900000001", "5511900000001@s.whatsapp.net"])
def test_on_invariant_a_the_author_is_silence_unless_a_phone_jid(participant_alt):
    record = {"key": _key("ID", participantAlt=participant_alt),
              "messageType": "conversation", "message": {"conversation": "olá"}, "contextInfo": None}
    out = EvolutionChannel(_cfg(True)).parse_inbound(_upsert(record))
    if participant_alt.endswith("@s.whatsapp.net"):
        assert isinstance(out, InboundMessage) and out.author == participant_alt
    else:
        assert out is None                       # @lid / empty / other host → SILENCE


def test_on_a_group_with_no_participant_alt_is_silence():
    record = {"key": {"id": "ID", "fromMe": False, "remoteJid": ROOM, "participant": AUTHOR_LID,
                      "addressingMode": "lid"},        # participantAlt absent entirely
              "messageType": "conversation", "message": {"conversation": "olá"}, "contextInfo": None}
    assert EvolutionChannel(_cfg(True)).parse_inbound(_upsert(record)) is None


# ── ON: invariant (b) — a group message always carries the room as conversation_id ─────────────
@pytest.mark.parametrize("label", sorted(REAL_FOUR))
def test_on_invariant_b_conversation_id_is_always_the_room(label):
    msg = EvolutionChannel(_cfg(True)).parse_inbound(REAL_FOUR[label])
    assert isinstance(msg, InboundMessage)
    assert msg.conversation_id == msg.sender == ROOM != ""   # never "" → never read as a 1:1


# ── ON: the quoted id is exposed; mentions_bot is NOT set (it needs the bot's own LID) ──────────
def test_on_a_reply_exposes_the_quoted_stanza_id():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_REPLY)
    assert isinstance(msg, InboundMessage)
    assert msg.reply_to_message_id == "3EB00000000000000000000000000004"
    assert msg.reply_to_bot is False             # the gateway is pure; the HOST decides reply_to_bot


def test_on_a_plain_message_exposes_no_quoted_id():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_PLAIN)
    assert isinstance(msg, InboundMessage) and msg.reply_to_message_id == ""


def test_on_a_mention_only_message_has_no_quoted_id_and_no_mentions_bot():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_MENTION)
    assert isinstance(msg, InboundMessage)
    assert msg.reply_to_message_id == ""         # a mention carries no stanzaId
    assert msg.mentions_bot is False             # mention detection needs the bot's own LID


# ── ON: the mentioned LIDs are EXPOSED (mentioned_ids), verbatim; the host decides mentions_bot ──
def test_on_a_mention_exposes_the_mentioned_lids_verbatim():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_MENTION)
    assert isinstance(msg, InboundMessage)
    assert msg.mentioned_ids == (OTHER_LID,)     # the LID, @lid, never the phone — matched by the host
    assert msg.mentions_bot is False             # the pure parser does not know the bot's own LID


def test_on_reply_plus_mention_exposes_both_the_quoted_id_and_the_mentioned_lids():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_REPLY_MENTION)
    assert isinstance(msg, InboundMessage)
    assert msg.mentioned_ids == (OTHER_LID,)
    assert msg.reply_to_message_id == "3EB00000000000000000000000000002"


@pytest.mark.parametrize("label", ["plain", "reply"])
def test_on_a_message_with_no_mention_has_empty_mentioned_ids(label):
    msg = EvolutionChannel(_cfg(True)).parse_inbound(REAL_FOUR[label])
    assert isinstance(msg, InboundMessage) and msg.mentioned_ids == ()


def test_on_reply_plus_mention_exposes_the_quoted_id_and_still_no_mentions_bot():
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_REPLY_MENTION)
    assert isinstance(msg, InboundMessage)
    assert msg.reply_to_message_id == "3EB00000000000000000000000000002"
    assert msg.mentions_bot is False


# ── ON: the bot's own echo still drops in a group (key.fromMe) ─────────────────────────────────
def test_on_the_bots_own_echo_in_a_group_still_drops():
    record = dict(MSG_PLAIN["data"], key=_key("ECHO", fromMe=True))
    assert EvolutionChannel(_cfg(True)).parse_inbound(_upsert(record)) is None


def test_the_reserved_fields_a_group_message_sets_are_the_designed_ones():
    """A parsed group message fills exactly ``conversation_id``, ``author`` and (on a reply)
    ``reply_to_message_id``; it never fills ``mentions_bot``, ``reply_to_bot`` or ``author_is_bot``
    (those are the host's, not the pure parser's)."""
    msg = EvolutionChannel(_cfg(True)).parse_inbound(MSG_REPLY)
    filled = {f.name for f in dataclasses.fields(InboundMessage)
              if f.name in ("conversation_id", "author", "mentions_bot", "reply_to_bot",
                            "reply_to_message_id", "author_is_bot")
              and getattr(msg, f.name) not in ("", False, None)}
    assert filled == {"conversation_id", "author", "reply_to_message_id"}
