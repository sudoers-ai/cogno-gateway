"""The five group fields of ``InboundMessage`` are RESERVED, and a 1:1 message cannot tell they exist.

A group chat separates two things a 1:1 chat keeps together: WHERE a reply goes (the group) and
WHO spoke (one member). ``InboundMessage`` had only ``sender`` for both, so the model gained
``conversation_id``, ``author``, ``mentions_bot``, ``reply_to_bot`` and ``author_is_bot``. Nothing
fills them yet, and every adapter still DROPS group traffic at parse, so the only messages a host
can receive are 1:1 messages — and for those this file proves the change is invisible:

* **the representation** — every message the four adapters parse out of a fixed 1:1 corpus prints
  byte for byte as it did on the tree before the fields (``_BASE_REPR``, digests taken there);
* **the serialisation** — ``dataclasses.asdict`` is the one place the fields DO appear (a dataclass
  field cannot leave it), so it is pinned exactly: the five reserved keys, at their default, and
  every other key byte for byte the old one (``_BASE_ASDICT``);
* **the shape** — positional construction, ``__match_args__`` and the order of the old fields are
  the old ones (the new fields are keyword-only);
* **the drop** — a group message of each channel that has groups still parses to ``None``, beside
  its 1:1 twin that still parses. WhatsApp Cloud joined it in 0.1.2: a Groups-API reply carries a
  ``group_id`` in the message object, and the adapter now drops it; a ``group_id`` that names no
  group (absent, ``null``, empty, blank) stays a 1:1 message, byte for byte as before
  (``_CLOUD_CONTROL_BASE``, digests taken on ``6e7dc71``).

The digests are a landing-time proof, taken on ``3ff003b`` (the ``main`` this change starts from):
regenerate them only when a parser's 1:1 output changes ON PURPOSE.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    InboundMessage,
    MessageKind,
    TelegramChannel,
    WebChannel,
    WhatsAppCloudChannel,
)

GROUP_FIELDS = ("conversation_id", "author", "mentions_bot", "reply_to_bot", "author_is_bot")
DEFAULTS = {"conversation_id": "", "author": "", "mentions_bot": False, "reply_to_bot": False,
            "author_is_bot": False}

# The fields as they were, in order — the positional shape a caller may rely on.
OLD_FIELDS = ("channel", "sender", "sender_user_id", "kind", "message_id", "text", "media",
              "reaction", "location", "selection", "reply_to", "raw")

_TG = TelegramChannel(ChannelConfig(token="BOT123", secret="sek"))
_EVO = EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1"))
_CLOUD = WhatsAppCloudChannel(ChannelConfig(token="ACCESS", instance="PHONE_ID", secret="appsecret",
                                            extra={"verify_token": "vtok"}))
_WEB = WebChannel()


def _upsert(**data):
    return {"event": "messages.upsert", "data": data}


def _cloud(message: dict, contacts: "list | None" = None) -> dict:
    value: dict = {"messages": [message]}
    if contacts is not None:
        value["contacts"] = contacts
    return {"entry": [{"changes": [{"value": value}]}]}


# A fixed 1:1 corpus: every kind each adapter parses, with invented ids and texts.
CORPUS: "dict[str, tuple]" = {
    "tg_text_reply": (_TG, {"message": {"chat": {"id": 42, "type": "private"}, "message_id": 7,
                                        "text": "oi", "reply_to_message": {"text": "antes"}}}),
    "tg_voice": (_TG, {"message": {"chat": {"id": 1}, "message_id": 8,
                                   "voice": {"file_id": "FID", "mime_type": "audio/ogg"}}}),
    "tg_photo": (_TG, {"message": {"chat": {"id": 1}, "caption": "olha",
                                   "photo": [{"file_id": "small"}, {"file_id": "big"}]}}),
    "tg_document": (_TG, {"message": {"chat": {"id": 1}, "document": {
        "file_id": "D", "file_name": "a.pdf", "mime_type": "application/pdf"}}}),
    "tg_sticker": (_TG, {"message": {"chat": {"id": 1}, "sticker": {"file_id": "S"}}}),
    "tg_callback": (_TG, {"callback_query": {"data": "confirm_yes",
                                             "message": {"chat": {"id": 7}, "message_id": 50}}}),
    "tg_reaction": (_TG, {"message_reaction": {"chat": {"id": 5, "type": "private"},
                                               "message_id": 99,
                                               "new_reaction": [{"type": "emoji", "emoji": "❤"}]}}),
    "evo_conversation": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A1"},
                                       messageType="conversation", message={"conversation": "olá"})),
    "evo_extended": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A2"},
                                   messageType="extendedTextMessage",
                                   message={"extendedTextMessage": {"text": "link http://x.y"}})),
    "evo_lid": (_EVO, _upsert(key={"remoteJid": "123456789012345@lid", "id": "A3"},
                              messageType="conversation", message={"conversation": "oi"})),
    "evo_reaction": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A4"},
                                   message={"reactionMessage": {"text": "👍", "key": {"id": "Z9"}}})),
    "evo_image": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A5"},
                                messageType="imageMessage",
                                message={"imageMessage": {"caption": "foto", "mimetype": "image/jpeg",
                                                          "url": "https://mmg.example/x"}})),
    "evo_buttons": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A6"},
                                  message={"buttonsResponseMessage": {"selectedButtonId": "yes",
                                                                      "selectedDisplayText": "Sim"}})),
    "evo_list": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A7"},
                               message={"listResponseMessage": {
                                   "title": "Corte", "singleSelectReply": {"selectedRowId": "c1"}}})),
    "evo_unknown": (_EVO, _upsert(key={"remoteJid": "5511900000001@s.whatsapp.net", "id": "A8"},
                                  messageType="pollCreationMessage", message={})),
    "cloud_text": (_CLOUD, _cloud({"from": "5511", "id": "wamid.1", "type": "text",
                                   "text": {"body": "oi"}})),
    "cloud_text_bsuid": (_CLOUD, _cloud({"from": "5511", "id": "w1", "type": "text",
                                         "text": {"body": "oi"}, "from_user_id": "BSUID_1"},
                                        [{"wa_id": "5511", "user_id": "BSUID_1"}])),
    "cloud_interactive": (_CLOUD, _cloud({"from": "5511", "id": "x", "type": "interactive",
                                          "interactive": {"type": "button_reply", "button_reply": {
                                              "id": "yes", "title": "Sim"}}})),
    "cloud_template_button": (_CLOUD, _cloud({"from": "5511", "id": "x", "type": "button",
                                              "button": {"payload": "P", "text": "Confirmar"}})),
    "cloud_reaction": (_CLOUD, _cloud({"from": "5511", "id": "x", "type": "reaction",
                                       "reaction": {"emoji": "👍", "message_id": "wamid.0"}})),
    "cloud_location": (_CLOUD, _cloud({"from": "5511", "id": "l", "type": "location",
                                       "location": {"latitude": -23.5, "longitude": -46.6,
                                                    "name": "Praça"}})),
    "cloud_image": (_CLOUD, _cloud({"from": "5511", "id": "i", "type": "image",
                                    "image": {"id": "MID", "mime_type": "image/jpeg",
                                              "caption": "c"}})),
    "cloud_unknown": (_CLOUD, _cloud({"from": "5511", "id": "u", "type": "order"})),
    "web_text": (_WEB, {"session_id": "s1", "message": "olá", "message_id": "m1"}),
    "web_reaction": (_WEB, {"session_id": "s1",
                            "reaction": {"emoji": "👍", "target_message_id": "m0"}}),
    "web_media": (_WEB, {"session_id": "s1", "kind": "image", "message": "veja",
                         "media": {"url": "https://x.example/a.png", "mime": "image/png"}}),
}


def _parse(label: str) -> InboundMessage:
    channel, payload = CORPUS[label]
    out = channel.parse_inbound(payload)
    msg = out[0] if isinstance(out, list) else out
    assert isinstance(msg, InboundMessage), label
    return msg


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _asdict_without_group_fields(msg: InboundMessage) -> str:
    d = {k: v for k, v in dataclasses.asdict(msg).items() if k not in GROUP_FIELDS}
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


# Taken on 3ff003b — `repr(msg)` and `asdict(msg)` (JSON, sorted keys) of each parsed message.
_BASE_REPR: "dict[str, str]" = {
    "cloud_image": "8735fb1a4e462577",
    "cloud_interactive": "58af597423c9333f",
    "cloud_location": "22c06f66e47082eb",
    "cloud_reaction": "80767f1d30ad65bf",
    "cloud_template_button": "ea4aad717603ffa7",
    "cloud_text": "07c3d8d33e28dccf",
    "cloud_text_bsuid": "a76d8aa14ae9a4f8",
    "cloud_unknown": "543a47d438ffebce",
    "evo_buttons": "a3b56462987528ac",
    "evo_conversation": "e289ce44d20f5495",
    "evo_extended": "b4e6b373543d4bf5",
    "evo_image": "29561b56c8fad5ce",
    "evo_lid": "2754404df8519f85",
    "evo_list": "f0b61c455b69f231",
    "evo_reaction": "48b39d20a7ef90d4",
    "evo_unknown": "37d5add39065425f",
    "tg_callback": "232b47bae7a25a12",
    "tg_document": "0c5aaeaee7020769",
    "tg_photo": "feb543c1f1ef335d",
    "tg_reaction": "8773339834ce44b8",
    "tg_sticker": "3a6f3bf5cf0691dc",
    "tg_text_reply": "98c5150fbc28035d",
    "tg_voice": "9c244061ad2bfc61",
    "web_media": "ba0d5b0e9c6f3113",
    "web_reaction": "b377911cdc188f77",
    "web_text": "477b5fff1816a794",
}
_BASE_ASDICT: "dict[str, str]" = {
    "cloud_image": "18db2e118fb6ed3b",
    "cloud_interactive": "5cc1c479fa271ec8",
    "cloud_location": "fbecf0169455e1cf",
    "cloud_reaction": "07ed81fbf7df0a1f",
    "cloud_template_button": "5cec58a0685b1e0b",
    "cloud_text": "535e2b605c14c62b",
    "cloud_text_bsuid": "70612a6eed1113e5",
    "cloud_unknown": "1a4ea0aac03c6c56",
    "evo_buttons": "002a1cad6fcf6b84",
    "evo_conversation": "a0d3753486f3f484",
    "evo_extended": "7b0e4dbdf41ccb56",
    "evo_image": "358d6ace19d7c1e6",
    "evo_lid": "cd8f127d62ae55a5",
    "evo_list": "3d77ef34412a3a63",
    "evo_reaction": "f437879b2c6360f9",
    "evo_unknown": "a05e36b417d19a47",
    "tg_callback": "7eff2c5be68c91dc",
    "tg_document": "7cbbef768fc447ee",
    "tg_photo": "2ef8b2013eb99994",
    "tg_reaction": "b3b83b5bfb45616a",
    "tg_sticker": "5a8922e97116fc18",
    "tg_text_reply": "01e1f0d6580d2103",
    "tg_voice": "b519bc769b30ff34",
    "web_media": "398b9e714a91fb3b",
    "web_reaction": "00bb98d49caa78d5",
    "web_text": "0eb2e970a8eac8da",
}


def test_the_corpus_is_read_and_the_digests_cover_it():
    """The control: a corpus that parsed nothing would make every digest test pass over nothing."""
    assert len(CORPUS) == 26
    assert set(_BASE_REPR) == set(CORPUS) == set(_BASE_ASDICT)
    kinds = {_parse(label).kind for label in CORPUS}
    assert {MessageKind.TEXT, MessageKind.AUDIO, MessageKind.IMAGE, MessageKind.DOCUMENT,
            MessageKind.REACTION, MessageKind.INTERACTIVE, MessageKind.LOCATION,
            MessageKind.UNKNOWN} <= kinds
    # all four adapters (the Cloud adapter also names its messages "whatsapp")
    assert {type(CORPUS[label][0]) for label in CORPUS} == {
        TelegramChannel, EvolutionChannel, WhatsAppCloudChannel, WebChannel}
    assert {_parse(label).channel for label in CORPUS} == {"telegram", "whatsapp", "web"}


@pytest.mark.parametrize("label", sorted(CORPUS))
def test_a_1to1_message_prints_byte_for_byte_as_before(label):
    assert _digest(repr(_parse(label))) == _BASE_REPR[label]


@pytest.mark.parametrize("label", sorted(CORPUS))
def test_asdict_gains_the_five_reserved_keys_at_their_default_and_nothing_else(label):
    msg = _parse(label)
    d = dataclasses.asdict(msg)
    assert {k: d[k] for k in GROUP_FIELDS} == DEFAULTS
    assert _digest(_asdict_without_group_fields(msg)) == _BASE_ASDICT[label]


@pytest.mark.parametrize("label", sorted(CORPUS))
def test_no_adapter_fills_a_group_field_yet(label):
    msg = _parse(label)
    assert {name: getattr(msg, name) for name in GROUP_FIELDS} == DEFAULTS


def test_the_group_fields_exist_with_the_1to1_defaults_and_are_keyword_only():
    msg = InboundMessage(channel="telegram", sender="42")
    assert {name: getattr(msg, name) for name in GROUP_FIELDS} == DEFAULTS
    by_name = {f.name: f for f in dataclasses.fields(InboundMessage)}
    assert all(by_name[name].kw_only for name in GROUP_FIELDS)
    # the old fields keep their order and are still the only positional ones
    assert tuple(f.name for f in dataclasses.fields(InboundMessage))[:len(OLD_FIELDS)] == OLD_FIELDS
    assert InboundMessage.__match_args__ == OLD_FIELDS
    positional = InboundMessage("telegram", "42", "", MessageKind.TEXT, "7", "oi")
    assert positional == InboundMessage(channel="telegram", sender="42", message_id="7", text="oi")


def test_the_repr_hides_a_group_field_only_while_it_holds_its_default():
    from cogno_gateway.types import _GROUP_FIELDS

    assert _GROUP_FIELDS == GROUP_FIELDS
    msg = InboundMessage(channel="telegram", sender="-1001", conversation_id="-1001", author="7",
                         mentions_bot=True)
    text = repr(msg)
    assert "conversation_id='-1001'" in text and "author='7'" in text
    assert "mentions_bot=True" in text
    assert "reply_to_bot" not in text and "author_is_bot" not in text
    # control: the same message without them prints none of the five
    assert not any(name in repr(InboundMessage(channel="telegram", sender="-1001"))
                   for name in GROUP_FIELDS)


def test_replace_carries_the_group_fields_through():
    """A host rewrites a message with ``dataclasses.replace`` (the WhatsApp sender is collapsed to
    its digits that way). A field that ``replace`` resets — an ``InitVar`` with a default would —
    would lose the group the moment a sender is normalised."""
    msg = InboundMessage(channel="whatsapp", sender="5511900000001@s.whatsapp.net",
                         conversation_id="120363000000000001@g.us", author="5511900000001",
                         reply_to_bot=True, author_is_bot=False)
    moved = dataclasses.replace(msg, sender="5511900000001")
    assert (moved.conversation_id, moved.author, moved.reply_to_bot) == (
        "120363000000000001@g.us", "5511900000001", True)


# ── the drop: a group message of each channel still never becomes a message ─────────────────
# WhatsApp Cloud marks a group message ONLY by a `group_id` inside the message object, under the
# same `messages` webhook field as a 1:1 message, with `from` set to the participant: "The
# `message` object includes a `group_id` field to indicate this is a group message" (Meta,
# https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/groups-messaging/
# read 2026-10-21). Every kind the adapter parses, so the drop cannot be a branch of one of them.
_CLOUD_GROUP_ID = "HBgLGRUPOINVENTADO"
_CLOUD_GROUP_SHAPES: "dict[str, dict]" = {
    "cloud_group_text": {"from": "5511900000002", "id": "wamid.g1", "type": "text",
                         "text": {"body": "oi"}},
    "cloud_group_interactive": {"from": "5511900000002", "id": "wamid.g2", "type": "interactive",
                                "interactive": {"type": "button_reply",
                                                "button_reply": {"id": "yes", "title": "Sim"}}},
    "cloud_group_template_button": {"from": "5511900000002", "id": "wamid.g3", "type": "button",
                                    "button": {"payload": "P", "text": "Confirmar"}},
    "cloud_group_reaction": {"from": "5511900000002", "id": "wamid.g4", "type": "reaction",
                             "reaction": {"emoji": "👍", "message_id": "wamid.0"}},
    "cloud_group_location": {"from": "5511900000002", "id": "wamid.g5", "type": "location",
                             "location": {"latitude": -23.5, "longitude": -46.6}},
    "cloud_group_image": {"from": "5511900000002", "id": "wamid.g6", "type": "image",
                          "image": {"id": "MID", "mime_type": "image/jpeg", "caption": "c"}},
    "cloud_group_unknown": {"from": "5511900000002", "id": "wamid.g7", "type": "order"},
}

GROUP_DROPS: "dict[str, tuple]" = {
    "tg_group_text": (_TG, {"message": {"chat": {"id": -1001, "type": "group"}, "message_id": 7,
                                        "from": {"id": 7, "is_bot": False},
                                        "text": "@algum_bot marca amanhã",
                                        "entities": [{"type": "mention", "offset": 0, "length": 10}]}}),
    "tg_supergroup_text": (_TG, {"message": {"chat": {"id": -1002, "type": "supergroup"},
                                             "message_id": 8, "from": {"id": 8, "is_bot": False},
                                             "text": "alguém?"}}),
    "tg_supergroup_reply_to_bot": (_TG, {"message": {
        "chat": {"id": -1002, "type": "supergroup"}, "message_id": 9,
        "from": {"id": 8, "is_bot": False}, "text": "sim",
        "reply_to_message": {"text": "Confirma?", "from": {"id": 1, "is_bot": True}}}}),
    "tg_group_callback": (_TG, {"callback_query": {"data": "confirm_yes", "message": {
        "chat": {"id": -1001, "type": "group"}, "message_id": 50}}}),
    "tg_group_reaction": (_TG, {"message_reaction": {"chat": {"id": -1001, "type": "group"},
                                                     "message_id": 99,
                                                     "new_reaction": [{"type": "emoji",
                                                                       "emoji": "❤"}]}}),
    "evo_group_text": (_EVO, _upsert(key={"remoteJid": "120363000000000001@g.us", "id": "G1",
                                          "participant": "5511900000002@s.whatsapp.net"},
                                     messageType="extendedTextMessage",
                                     message={"extendedTextMessage": {
                                         "text": "@5511900000003 marca amanhã",
                                         "contextInfo": {"mentionedJid": [
                                             "5511900000003@s.whatsapp.net"]}}})),
    "evo_group_old_id": (_EVO, _upsert(key={"remoteJid": "551190000000-1600000000@g.us", "id": "G2",
                                            "participant": "5511900000002@s.whatsapp.net"},
                                       messageType="conversation", message={"conversation": "oi"})),
    **{label: (_CLOUD, _cloud(dict(message, group_id=_CLOUD_GROUP_ID)))
       for label, message in _CLOUD_GROUP_SHAPES.items()},
}

# The SAME shapes with the group taken out — the twin that proves the drop is about the group.
ONE_TO_ONE_TWINS: "dict[str, tuple]" = {
    "tg_group_text": (_TG, {"message": {"chat": {"id": 7, "type": "private"}, "message_id": 7,
                                        "from": {"id": 7, "is_bot": False},
                                        "text": "@algum_bot marca amanhã"}}),
    "tg_supergroup_text": (_TG, {"message": {"chat": {"id": 8, "type": "private"}, "message_id": 8,
                                             "text": "alguém?"}}),
    "tg_supergroup_reply_to_bot": (_TG, {"message": {
        "chat": {"id": 8, "type": "private"}, "message_id": 9, "text": "sim",
        "reply_to_message": {"text": "Confirma?", "from": {"id": 1, "is_bot": True}}}}),
    "tg_group_callback": (_TG, {"callback_query": {"data": "confirm_yes", "message": {
        "chat": {"id": 7, "type": "private"}, "message_id": 50}}}),
    "tg_group_reaction": (_TG, {"message_reaction": {"chat": {"id": 7, "type": "private"},
                                                     "message_id": 99,
                                                     "new_reaction": [{"type": "emoji",
                                                                       "emoji": "❤"}]}}),
    "evo_group_text": (_EVO, _upsert(key={"remoteJid": "5511900000002@s.whatsapp.net", "id": "G1"},
                                     messageType="extendedTextMessage",
                                     message={"extendedTextMessage": {"text": "marca amanhã"}})),
    "evo_group_old_id": (_EVO, _upsert(key={"remoteJid": "5511900000002@s.whatsapp.net", "id": "G2"},
                                       messageType="conversation", message={"conversation": "oi"})),
    # the Cloud twin is the same message object without its `group_id`
    **{label: (_CLOUD, _cloud(message)) for label, message in _CLOUD_GROUP_SHAPES.items()},
}


@pytest.mark.parametrize("label", sorted(GROUP_DROPS))
def test_a_group_message_still_drops_and_its_1to1_twin_still_parses(label):
    channel, payload = GROUP_DROPS[label]
    assert channel.parse_inbound(payload) is None
    twin_channel, twin_payload = ONE_TO_ONE_TWINS[label]
    assert isinstance(twin_channel.parse_inbound(twin_payload), InboundMessage)


def test_a_group_presence_still_never_moves_a_deadline():
    payload = {"event": "presence.update", "data": {
        "id": "120363000000000001@g.us",
        "presences": {"5511900000002@s.whatsapp.net": {"lastKnownPresence": "composing"}}}}
    assert _EVO.parse_presence(payload) is None
    twin = {"event": "presence.update", "data": {
        "id": "5511900000002@s.whatsapp.net",
        "presences": {"5511900000002@s.whatsapp.net": {"lastKnownPresence": "composing"}}}}
    assert _EVO.parse_presence(twin) is not None


def test_every_cloud_group_shape_is_in_the_drop_table_with_a_twin():
    """The control for the parametrised drop above: the Cloud shapes are IN it, each beside a twin
    that differs from it by the `group_id` alone."""
    for label, message in _CLOUD_GROUP_SHAPES.items():
        assert label in GROUP_DROPS and label in ONE_TO_ONE_TWINS
        group = GROUP_DROPS[label][1]["entry"][0]["changes"][0]["value"]["messages"][0]
        twin = ONE_TO_ONE_TWINS[label][1]["entry"][0]["changes"][0]["value"]["messages"][0]
        assert group == dict(twin, group_id=_CLOUD_GROUP_ID) and "group_id" not in twin


def test_a_cloud_group_reply_drops_without_dropping_the_rest_of_its_delivery(caplog):
    """Meta batches several messages, entries and changes into one POST. The group reply is the
    only thing that leaves: the 1:1 messages around it parse, in order, and the drop is not logged
    as a parse failure (it is not one — the other adapters drop a group silently too)."""
    group = dict(_CLOUD_GROUP_SHAPES["cloud_group_text"], group_id=_CLOUD_GROUP_ID)
    first = {"from": "5511900000004", "id": "wamid.a", "type": "text", "text": {"body": "um"}}
    beside = {"from": "5511900000006", "id": "wamid.c", "type": "text", "text": {"body": "tres"}}
    last = {"from": "5511900000005", "id": "wamid.b", "type": "text", "text": {"body": "dois"}}
    payload = {"entry": [{"changes": [{"value": {"messages": [first, group, beside]}}]},
                         {"changes": [{"value": {"messages": [last]}}]}]}
    with caplog.at_level("DEBUG", logger="cogno_gateway.cloud"):
        batch = _CLOUD.parse_batch(payload)
    assert [(m.sender, m.message_id, m.text) for m in batch] == [
        ("5511900000004", "wamid.a", "um"), ("5511900000006", "wamid.c", "tres"),
        ("5511900000005", "wamid.b", "dois")]
    assert "parse_message_failed" not in caplog.text
    # a delivery whose only message is a group reply: nothing, and parse_inbound agrees
    only = _cloud(group)
    assert _CLOUD.parse_batch(only) == [] and _CLOUD.parse_inbound(only) is None


# A `group_id` that names no group is the 1:1 path. Meta's page describes the field only as the
# marker of a group message and the group id as what a reply is addressed to; it says nothing of an
# empty one. An empty or blank value names no group to answer, this adapter already reads a
# present-but-null field as absent, and the costs are not symmetric: dropping a real 1:1 message on
# a blank key loses it with nobody told, keeping it is what every message got before.
_CLOUD_CONTROL_MESSAGE = {"from": "5511900000002", "id": "wamid.c1", "type": "text",
                          "text": {"body": "oi"}}
_CLOUD_CONTROLS: "dict[str, dict]" = {
    "absent": dict(_CLOUD_CONTROL_MESSAGE),
    "empty": dict(_CLOUD_CONTROL_MESSAGE, group_id=""),
    "null": dict(_CLOUD_CONTROL_MESSAGE, group_id=None),
    "blank": dict(_CLOUD_CONTROL_MESSAGE, group_id="   "),
}
# Taken on 6e7dc71 (before the Cloud drop): (`repr`, `asdict` as sorted JSON) of each control.
_CLOUD_CONTROL_BASE: "dict[str, tuple[str, str]]" = {
    "absent": ("8fa5ddc59ba27235", "0cd70e9c71f2f551"),
    "empty": ("62200a04bdcd136a", "9e90485693df8999"),
    "null": ("689ed424fc91745c", "0b145e48e6db2a7a"),
    "blank": ("cf9d4116d136e516", "2306b0dbf429e487"),
}


@pytest.mark.parametrize("label", sorted(_CLOUD_CONTROLS))
def test_a_cloud_group_id_that_names_no_group_stays_a_1to1_message_byte_for_byte(label):
    msg = _CLOUD.parse_inbound(_cloud(_CLOUD_CONTROLS[label]))
    assert isinstance(msg, InboundMessage)
    as_json = json.dumps(dataclasses.asdict(msg), sort_keys=True, ensure_ascii=False, default=str)
    assert (_digest(repr(msg)), _digest(as_json)) == _CLOUD_CONTROL_BASE[label]
    assert (msg.sender, msg.text) == ("5511900000002", "oi")
