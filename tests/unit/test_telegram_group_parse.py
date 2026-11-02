"""Telegram group chats, Phase 1: the parse fills the group fields, but ONLY when the host opts in.

``ChannelConfig.accept_groups`` is the switch, and its default is ``False``. With it off, every
group shape drops at parse exactly as before: message, button tap and reaction, ``group`` and
``supergroup``. With it on, a group ``message`` parses with:

* ``sender`` and ``conversation_id`` set to the ROOM's ``chat.id``. Telegram numbers messages PER
  CHAT, so a host that deduplicates on ``(sender, message_id)`` stays correct only if ``sender`` is
  the room. Keyed by the author, two rooms' message 57 from the same member would collide;
* ``author`` set to the member's ``from.id``, kept apart from the room;
* ``mentions_bot`` read from the ``entities`` (or ``caption_entities``), whose offsets are in
  UTF-16 code units, not Python code points;
* ``reply_to_bot`` true when the message replies to one of this bot's messages;
* ``author_is_bot`` true when Telegram says the author is a bot.

Still dropped with the switch on: a group button tap or reaction (Phase 1 sends no buttons), a
message posted on behalf of a chat (``sender_chat``, an anonymous admin or a linked channel), a
message with no ``from``, and a forum supergroup (``is_forum``, whose topics this phase does not
address).

Every name, id and handle below is invented.
"""

from __future__ import annotations

import pytest

from cogno_gateway import ChannelConfig, OutboundMessage, TelegramChannel
from tests.conftest import FakeResponse, body_of

BOT_ID = 7000001
HANDLE = "assistente_teste_bot"
ROOM = -1001234567890
OTHER_ROOM = -1009876543210
MEMBER = 5550001
OTHER_MEMBER = 5550002


def _on(**over) -> TelegramChannel:
    cfg = dict(token=f"{BOT_ID}:SEGREDO-INVENTADO", secret="sek", accept_groups=True,
               bot_username=f"@{HANDLE}")
    cfg.update(over)
    return TelegramChannel(ChannelConfig(**cfg))


def _off() -> TelegramChannel:
    return TelegramChannel(ChannelConfig(token=f"{BOT_ID}:SEGREDO-INVENTADO", secret="sek"))


def _u16(text: str) -> int:
    """The length of ``text`` in UTF-16 code units — the unit of a Telegram entity offset."""
    return len(text.encode("utf-16-le")) // 2


def _mention_entity(text: str, mention: str, kind: str = "mention") -> dict:
    head = text[: text.index(mention)]
    return {"type": kind, "offset": _u16(head), "length": _u16(mention)}


def _group_msg(text: str = "quanto custa a mensalidade?", *, room: int = ROOM,
               member: int = MEMBER, message_id: int = 57, chat_type: str = "supergroup",
               entities=None, **extra) -> dict:
    msg = {"message_id": message_id, "text": text,
           "chat": {"id": room, "type": chat_type, "title": "Turma Inventada"},
           "from": {"id": member, "is_bot": False, "first_name": "Membro"}}
    if entities is not None:
        msg["entities"] = entities
    msg.update(extra)
    return {"message": msg}


# ── the switch: default OFF, and OFF is today byte for byte ───────────────────────────────

def test_the_switch_defaults_to_off_and_names_no_bot():
    cfg = ChannelConfig()
    assert cfg.accept_groups is False
    assert cfg.bot_username == ""


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_with_the_switch_off_every_group_shape_still_drops(chat_type):
    text = f"@{HANDLE} oi"
    shapes = [
        _group_msg(text, chat_type=chat_type, entities=[_mention_entity(text, f"@{HANDLE}")]),
        {"callback_query": {"data": "sim",
                            "message": {"chat": {"id": ROOM, "type": chat_type},
                                        "message_id": 50}}},
        {"message_reaction": {"chat": {"id": ROOM, "type": chat_type}, "message_id": 50,
                              "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
    ]
    for payload in shapes:
        assert _off().parse_inbound(payload) is None


@pytest.mark.parametrize("payload", [
    {"message": {"chat": {"id": 42, "type": "private"}, "message_id": 7, "text": "oi",
                 "from": {"id": 42, "is_bot": False}}},
    {"message": {"chat": {"id": 42}, "message_id": 8, "text": "@assistente_teste_bot oi",
                 "entities": [{"type": "mention", "offset": 0, "length": 21}]}},
    {"message": {"chat": {"id": 42, "type": "private"}, "message_id": 9,
                 "voice": {"file_id": "F", "mime_type": "audio/ogg"}}},
    {"callback_query": {"data": "sim", "message": {"chat": {"id": 42}, "message_id": 50}}},
    {"message_reaction": {"chat": {"id": 42, "type": "private"}, "message_id": 50,
                          "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
], ids=["private_text", "typeless_text_with_mention", "private_voice", "private_tap",
        "private_reaction"])
def test_a_1to1_message_parses_the_same_with_the_switch_on(payload):
    """A host that turns groups on must not change a single 1:1 message: same ``repr``, which
    hides the group fields only while they hold their default."""
    off, on = _off().parse_inbound(payload), _on().parse_inbound(payload)
    assert off is not None and repr(on) == repr(off)
    assert on.conversation_id == "" and on.author == "" and on.mentions_bot is False


# ── the room and the author ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_a_group_message_carries_the_room_as_sender_and_the_member_as_author(chat_type):
    msg = _on().parse_inbound(_group_msg(chat_type=chat_type))
    assert msg is not None
    assert msg.sender == str(ROOM) and msg.conversation_id == str(ROOM)
    assert msg.author == str(MEMBER)
    assert msg.author_is_bot is False
    assert msg.mentions_bot is False and msg.reply_to_bot is False
    assert msg.message_id == "57" and msg.text == "quanto custa a mensalidade?"


def test_the_same_message_id_in_two_rooms_gives_two_distinct_delivery_keys():
    """The host's dedup keys on (sender, message_id). Telegram numbers per chat, so the same
    member's message 57 in two rooms must differ in ``sender`` — it would not if ``sender`` were
    the author."""
    a = _on().parse_inbound(_group_msg(room=ROOM, member=MEMBER, message_id=57))
    b = _on().parse_inbound(_group_msg(room=OTHER_ROOM, member=MEMBER, message_id=57))
    assert (a.sender, a.message_id) != (b.sender, b.message_id)
    assert a.author == b.author == str(MEMBER)


def test_a_bot_author_is_marked_and_still_parses_for_the_host_to_decide():
    payload = _group_msg()
    payload["message"]["from"] = {"id": 8880001, "is_bot": True, "username": "outro_bot"}
    msg = _on().parse_inbound(payload)
    assert msg is not None and msg.author_is_bot is True and msg.author == "8880001"


@pytest.mark.parametrize("shape", ["sender_chat", "no_from", "forum"])
def test_a_message_with_no_person_behind_it_or_in_a_forum_drops(shape):
    payload = _group_msg()
    m = payload["message"]
    if shape == "sender_chat":      # an anonymous admin, or a post forwarded from a linked channel
        m["sender_chat"] = {"id": ROOM, "type": "supergroup"}
        m["from"] = {"id": 1087968824, "is_bot": True, "username": "GroupAnonymousBot"}
    elif shape == "no_from":
        del m["from"]
    else:                           # forum topics are outside Phase 1
        m["chat"]["is_forum"] = True
    assert _on().parse_inbound(payload) is None
    # the control: the same payload without the shape parses
    assert _on().parse_inbound(_group_msg()) is not None


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_with_the_switch_on_a_group_tap_and_reaction_still_drop(chat_type):
    tap = {"callback_query": {"data": "sim", "from": {"id": MEMBER, "is_bot": False},
                              "message": {"chat": {"id": ROOM, "type": chat_type},
                                          "message_id": 50}}}
    react = {"message_reaction": {"chat": {"id": ROOM, "type": chat_type}, "message_id": 50,
                                  "user": {"id": MEMBER, "is_bot": False},
                                  "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}
    assert _on().parse_inbound(tap) is None
    assert _on().parse_inbound(react) is None


# ── mentions: UTF-16 offsets, the handle exactly, three entity kinds ──────────────────────

def test_a_mention_after_an_emoji_is_read_in_utf16_units():
    text = f"👋🏽 Olá @{HANDLE}, quanto custa?"
    ent = _mention_entity(text, f"@{HANDLE}")
    # the fixture DISCRIMINATES: a code-point slice at the same offset misses the handle
    assert text[ent["offset"]:ent["offset"] + ent["length"]] != f"@{HANDLE}"
    msg = _on().parse_inbound(_group_msg(text, entities=[ent]))
    assert msg.mentions_bot is True


@pytest.mark.parametrize("text,mention,expected", [
    (f"@{HANDLE} oi", f"@{HANDLE}", True),
    (f"oi @{HANDLE.upper()}", f"@{HANDLE.upper()}", True),
    (f"oi @{HANDLE}_falso", f"@{HANDLE}_falso", False),
    ("oi @outro_bot", "@outro_bot", False),
    (f"oi @{HANDLE[:-1]}", f"@{HANDLE[:-1]}", False),
], ids=["exact", "case_insensitive", "longer_handle", "another_bot", "prefix_of_handle"])
def test_only_this_bots_handle_is_a_mention(text, mention, expected):
    msg = _on().parse_inbound(_group_msg(text, entities=[_mention_entity(text, mention)]))
    assert msg.mentions_bot is expected


def test_the_handle_in_the_text_without_an_entity_is_not_a_mention():
    """Telegram marks a real mention with an entity; the handle inside a URL or a code span
    carries none. The text alone is never read."""
    msg = _on().parse_inbound(_group_msg(f"veja https://exemplo.invalid/@{HANDLE}"))
    assert msg.mentions_bot is False


def test_the_configured_handle_reads_the_same_with_or_without_the_at_sign():
    text = f"@{HANDLE} oi"
    ent = [_mention_entity(text, f"@{HANDLE}")]
    for handle in (f"@{HANDLE}", HANDLE, f"  @{HANDLE.upper()} "):
        assert _on(bot_username=handle).parse_inbound(_group_msg(text, entities=ent)).mentions_bot


def test_no_configured_handle_means_no_mention_ever():
    text = f"@{HANDLE} oi"
    msg = _on(bot_username="").parse_inbound(
        _group_msg(text, entities=[_mention_entity(text, f"@{HANDLE}")]))
    assert msg is not None and msg.mentions_bot is False


@pytest.mark.parametrize("user_id,expected", [(BOT_ID, True), (OTHER_MEMBER, False)],
                         ids=["this_bot", "another_user"])
def test_a_text_mention_counts_only_for_this_bots_id(user_id, expected):
    text = "Assistente, quanto custa?"
    ent = {"type": "text_mention", "offset": 0, "length": 10, "user": {"id": user_id}}
    assert _on().parse_inbound(_group_msg(text, entities=[ent])).mentions_bot is expected


@pytest.mark.parametrize("command,expected", [
    (f"/ajuda@{HANDLE}", True), ("/ajuda", False), ("/ajuda@outro_bot", False)],
    ids=["addressed_to_this_bot", "bare_command", "addressed_to_another_bot"])
def test_a_bot_command_counts_only_when_addressed_to_this_bot(command, expected):
    text = f"{command} horários"
    ent = {"type": "bot_command", "offset": 0, "length": _u16(command)}
    assert _on().parse_inbound(_group_msg(text, entities=[ent])).mentions_bot is expected


def test_a_caption_mention_is_read_from_caption_entities():
    caption = f"@{HANDLE} o que é isto?"
    payload = _group_msg()
    m = payload["message"]
    del m["text"]
    m["photo"] = [{"file_id": "small"}, {"file_id": "big"}]
    m["caption"] = caption
    m["caption_entities"] = [_mention_entity(caption, f"@{HANDLE}")]
    msg = _on().parse_inbound(payload)
    assert msg.mentions_bot is True and msg.media.ref == "big"
    # the twin: the same caption entities under the wrong key are not read
    m["entities"] = m.pop("caption_entities")
    assert _on().parse_inbound(payload).mentions_bot is False


@pytest.mark.parametrize("entities", [
    "not-a-list",
    [None, 3, "x"],
    [{"type": "mention", "offset": -1, "length": 21}],
    [{"type": "mention", "offset": 900, "length": 21}],
    [{"type": "mention", "offset": "0", "length": "21"}],
    [{"type": "mention", "offset": True, "length": 21}],
    [{"type": "text_mention", "offset": 0, "length": 3, "user": "x"}],
], ids=["string", "junk_items", "negative_offset", "past_the_end", "string_offsets",
        "bool_offset", "text_mention_without_user"])
def test_malformed_entities_never_raise_and_never_mention(entities):
    msg = _on().parse_inbound(_group_msg(f"@{HANDLE} oi", entities=entities))
    assert msg is not None and msg.mentions_bot is False


# ── replies to the bot ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("replied,expected", [
    ({"id": BOT_ID, "is_bot": True, "username": HANDLE}, True),
    ({"id": OTHER_MEMBER, "is_bot": False}, False),
    ({"id": 8880001, "is_bot": True, "username": "outro_bot"}, False),
], ids=["this_bot", "another_member", "another_bot"])
def test_a_reply_to_this_bots_message_is_marked(replied, expected):
    msg = _on().parse_inbound(_group_msg(
        "e para sábado?", reply_to_message={"message_id": 56, "text": "anterior",
                                            "from": replied}))
    assert msg.reply_to_bot is expected
    assert msg.reply_to == "anterior"


def test_a_token_without_a_numeric_id_falls_back_to_the_bots_handle():
    replied = {"id": 123, "is_bot": True, "username": HANDLE}
    payload = _group_msg("e para sábado?", reply_to_message={"message_id": 56, "from": replied})
    assert _on(token="BOT123").parse_inbound(payload).reply_to_bot is True
    # the twin: a PERSON whose username equals the handle is not the bot
    payload["message"]["reply_to_message"]["from"] = {"id": 123, "is_bot": False,
                                                      "username": HANDLE}
    assert _on(token="BOT123").parse_inbound(payload).reply_to_bot is False


def test_no_reply_is_no_reply_to_the_bot():
    assert _on().parse_inbound(_group_msg()).reply_to_bot is False


# ── the reply goes back threaded to the message that called the bot ───────────────────────

async def test_reply_to_message_id_threads_the_first_chunk_only(fake_httpx):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    long_text = ("Primeira parte da resposta. " * 40).strip()
    await _on(max_chars=200).send(str(ROOM), OutboundMessage(text=long_text,
                                                             reply_to_message_id="57"))
    sends = [body_of(c) for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert len(sends) > 1
    assert sends[0]["chat_id"] == str(ROOM)
    assert sends[0]["reply_parameters"] == {"message_id": 57,
                                            "allow_sending_without_reply": True}
    assert all("reply_parameters" not in s for s in sends[1:])


@pytest.mark.parametrize("value", ["", "abc", "-3", "0"], ids=["empty", "not_numeric",
                                                              "negative", "zero"])
async def test_no_usable_reply_id_sends_exactly_as_before(fake_httpx, value):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    await _on().send("42", OutboundMessage(text="oi", reply_to_message_id=value))
    body = body_of([c for c in fake_httpx.calls if "sendMessage" in c["url"]][-1])
    assert body == {"chat_id": "42", "text": "oi"}
