"""Telegram group chats, 0.1.6: THIS bot joined or left a room, as one closed event.

A host that wants to register a room the moment the bot is put in it needs to be told. Two
Telegram shapes say it, and the parse folds both into ``MessageKind.MEMBERSHIP`` with an
``InboundMessage.membership`` of ``joined`` or ``left``:

* the ``my_chat_member`` update, Telegram's own record that the bot's status changed. It is the
  ONLY one that reports a removal: a kicked bot receives nothing more from that room;
* the service messages every bot receives whatever its privacy mode: ``new_chat_members`` /
  ``left_chat_member`` naming the bot, ``group_chat_created``, and the two halves of a group's
  upgrade to a supergroup (``migrate_from_chat_id`` in the new room, ``migrate_to_chat_id`` in
  the old one).

``sender`` and ``conversation_id`` are the room; ``author`` stays empty (who added the bot is
nobody's business here); ``title`` is the room's name. Everything drops with the switch off, in a
private chat, in a forum, and when the member is not this bot.

Every name, id and handle below is invented.
"""

from __future__ import annotations

import dataclasses

import pytest

from cogno_gateway import (
    MEMBERSHIP_JOINED,
    MEMBERSHIP_LEFT,
    ChannelConfig,
    InboundMessage,
    Membership,
    MessageKind,
    TelegramChannel,
)

BOT_ID = 7000001
HANDLE = "assistente_teste_bot"
ROOM = -1001234567890
OLD_ROOM = -4000111222
ADDER = 5550001
OTHER = 5550002
TITLE = "Turma Inventada"


def _on(**over) -> TelegramChannel:
    cfg = dict(token=f"{BOT_ID}:SEGREDO-INVENTADO", secret="sek", accept_groups=True,
               bot_username=f"@{HANDLE}")
    cfg.update(over)
    return TelegramChannel(ChannelConfig(**cfg))


def _off() -> TelegramChannel:
    return TelegramChannel(ChannelConfig(token=f"{BOT_ID}:SEGREDO-INVENTADO", secret="sek"))


def _bot(**over) -> dict:
    user = {"id": BOT_ID, "is_bot": True, "first_name": "Assistente", "username": HANDLE}
    user.update(over)
    return user


def _person(uid: int = OTHER) -> dict:
    return {"id": uid, "is_bot": False, "first_name": "Pessoa"}


def _chat(room: int = ROOM, chat_type: str = "supergroup", **extra) -> dict:
    chat = {"id": room, "type": chat_type, "title": TITLE}
    chat.update(extra)
    return chat


def _my_chat_member(status: str, *, user=None, chat=None, old: str = "left", **member) -> dict:
    new = {"user": user if user is not None else _bot(), "status": status}
    new.update(member)
    return {"update_id": 1, "my_chat_member": {
        "chat": chat if chat is not None else _chat(), "from": _person(ADDER), "date": 1,
        "old_chat_member": {"user": new["user"], "status": old}, "new_chat_member": new}}


def _service(chat=None, **fields) -> dict:
    msg = {"message_id": 9, "date": 1, "chat": chat if chat is not None else _chat(),
           "from": _person(ADDER)}
    msg.update(fields)
    return {"update_id": 2, "message": msg}


def _joined(room: int = ROOM, title: str = TITLE) -> InboundMessage:
    return InboundMessage(channel="telegram", sender=str(room), kind=MessageKind.MEMBERSHIP,
                          conversation_id=str(room),
                          membership=Membership(status=MEMBERSHIP_JOINED, title=title))


def _left(room: int = ROOM, title: str = TITLE) -> InboundMessage:
    return InboundMessage(channel="telegram", sender=str(room), kind=MessageKind.MEMBERSHIP,
                          conversation_id=str(room),
                          membership=Membership(status=MEMBERSHIP_LEFT, title=title))


# ── my_chat_member ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,extra", [("member", {}), ("administrator", {}),
                                          ("restricted", {"is_member": True})],
                         ids=["member", "administrator", "restricted-member"])
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_the_bot_added_to_a_room_is_a_joined_membership(status, extra, chat_type):
    got = _on().parse_inbound(_my_chat_member(status, chat=_chat(chat_type=chat_type), **extra))
    assert got == _joined()


@pytest.mark.parametrize("status,extra", [("left", {}), ("kicked", {}),
                                          ("restricted", {"is_member": False})],
                         ids=["left", "kicked", "restricted-not-member"])
def test_the_bot_removed_from_a_room_is_a_left_membership(status, extra):
    got = _on().parse_inbound(_my_chat_member(status, old="member", **extra))
    assert got == _left()


def test_the_membership_carries_the_room_and_never_who_added_the_bot():
    got = _on().parse_inbound(_my_chat_member("member"))
    assert (got.sender, got.conversation_id) == (str(ROOM), str(ROOM))
    assert got.author == "" and got.raw == {} and got.message_id == "" and got.text == ""
    assert str(ADDER) not in repr(got) and str(ADDER) not in repr(dataclasses.asdict(got))


def test_control_another_members_status_is_not_the_bots():
    """The update is about a PERSON (``chat_member`` shape under the bot's key): not the bot."""
    assert _on().parse_inbound(_my_chat_member("member", user=_person())) is None


def test_control_a_person_named_like_the_bot_is_not_the_bot():
    """By id when the token carries one: a person whose username equals the handle is a person."""
    impostor = {"id": OTHER, "is_bot": False, "first_name": "X", "username": HANDLE}
    assert _on().parse_inbound(_my_chat_member("member", user=impostor)) is None


def test_the_bot_is_recognised_by_its_handle_when_the_token_has_no_id():
    channel = _on(token="SEM-ID:SEGREDO-INVENTADO")
    assert channel.parse_inbound(_my_chat_member("member", user=_bot(id=1))) == _joined()


@pytest.mark.parametrize("payload", [
    _my_chat_member("member", chat=_chat(chat_type="private")),   # a 1:1 chat: blocked/unblocked
    _my_chat_member("member", chat=_chat(chat_type="channel")),
    _my_chat_member("member", chat=_chat(is_forum=True)),         # Phase 1 does not serve forums
    _my_chat_member("creator"),                                   # not a status a bot can hold
    _my_chat_member("member", chat={"type": "supergroup"}),       # no room id
    {"my_chat_member": "not-an-object"},
], ids=["private", "channel", "forum", "creator", "no-room-id", "garbled"])
def test_shapes_that_are_not_a_room_membership_drop(payload):
    assert _on().parse_inbound(payload) is None


def test_a_title_that_is_not_text_reads_empty():
    got = _on().parse_inbound(_my_chat_member("member", chat=_chat(title=["x"])))
    assert got == _joined(title="")


# ── the service messages ──────────────────────────────────────────────────────────────────

def test_new_chat_members_naming_the_bot_is_a_join():
    got = _on().parse_inbound(_service(new_chat_members=[_person(), _bot()]))
    assert got == _joined()


def test_a_service_join_in_a_forum_drops():
    """A forum supergroup's topics are not addressed in Phase 1: no room is announced for it,
    from the service message either (``my_chat_member`` has its own twin above)."""
    assert _on().parse_inbound(_service(chat=_chat(is_forum=True),
                                        new_chat_members=[_bot()])) is None


def test_left_chat_member_naming_the_bot_is_a_leave():
    assert _on().parse_inbound(_service(left_chat_member=_bot())) == _left()


def test_group_chat_created_is_a_join():
    got = _on().parse_inbound(_service(chat=_chat(chat_type="group"), group_chat_created=True))
    assert got == _joined()


def test_an_upgrade_to_a_supergroup_joins_the_new_room_and_leaves_the_old_one():
    new_room = _on().parse_inbound(_service(migrate_from_chat_id=OLD_ROOM))
    old_room = _on().parse_inbound(_service(chat=_chat(room=OLD_ROOM, chat_type="group"),
                                            migrate_to_chat_id=ROOM))
    assert new_room == _joined(room=ROOM)
    assert old_room == _left(room=OLD_ROOM)


@pytest.mark.parametrize("fields", [
    {"new_chat_members": [_person()]},      # somebody ELSE joined
    {"left_chat_member": _person()},        # somebody ELSE left
    {"group_chat_created": False},
], ids=["other-joined", "other-left", "created-false"])
def test_control_a_service_message_about_somebody_else_is_not_a_membership(fields):
    """It parses as it did before 0.1.6: a group message of kind UNKNOWN, never a membership."""
    got = _on().parse_inbound(_service(**fields))
    assert got is not None and got.kind is MessageKind.UNKNOWN and got.membership is None


# ── the switch ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    _my_chat_member("member"), _my_chat_member("kicked", old="member"),
    _service(new_chat_members=[_bot()]), _service(left_chat_member=_bot()),
    _service(chat=_chat(chat_type="group"), group_chat_created=True),
    _service(migrate_from_chat_id=OLD_ROOM),
], ids=["mcm-join", "mcm-kick", "svc-join", "svc-leave", "svc-created", "svc-migrated"])
def test_with_the_switch_off_every_membership_shape_drops(payload):
    assert _off().parse_inbound(payload) is None


def test_the_membership_field_is_reserved_and_hidden_at_its_default():
    msg = InboundMessage(channel="telegram", sender="42")
    assert msg.membership is None and "membership" not in repr(msg)
    by_name = {f.name: f for f in dataclasses.fields(InboundMessage)}
    assert by_name["membership"].kw_only
    assert "membership=Membership(status='joined'" in repr(_joined())
