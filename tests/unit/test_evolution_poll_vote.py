"""Evolution parses a WhatsApp poll vote (``pollUpdateMessage``) into a ``POLL_VOTE`` message.

The provider delivers a vote DECRYPTED in ``messages.upsert``: it fetches the message that
created the poll, lifts the secret, and writes the chosen option NAMES in clear under
``pollUpdateMessage.vote.selectedOptions``, while ``pollUpdateMessage.pollCreationMessageKey.id``
names the creating message. The shapes below reproduce that nesting; every id, name and text is
synthetic (a "Qual turno prefere?" poll with "Manhã" / "Tarde" / "Noite").

What the twins pin:

* a vote with one chosen name, and with several, becomes ``selected`` as a tuple of those names;
* an EMPTY ``selectedOptions``, a MISSING ``vote``, and names that are not strings all parse to
  ``selected=None`` — UNREAD, never an abstention (an empty WhatsApp vote is ambiguous: a
  withdrawal, or a decryption that failed);
* a missing ``pollCreationMessageKey`` leaves ``poll_message_id=""`` (the host cannot look it up);
* in a group the author resolves from ``participantAlt`` (invariant a) and the room is carried as
  ``conversation_id`` (invariant b); without a phone ``participantAlt`` the vote is dropped;
* the bot's own echo (``fromMe``) is dropped;
* CONTROL: a reaction, a text and a media message still parse exactly as before, with
  ``poll_vote=None`` and their own kind — the new branch disturbs nothing beside it.
"""

from __future__ import annotations

import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    MediaRef,
    MessageKind,
    PollVote,
)

CFG = ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1")
ROOM = "120363000000000042@g.us"
AUTHOR_LID = "100000000000077@lid"
AUTHOR_PHONE = "5511955500077@s.whatsapp.net"
CREATION_ID = "3EB0QUOSTRALPOLLCREATION0001"
VOTE_ID = "3EB0QUOSTRALPOLLVOTE0001"


def _ch(accept_groups: bool = False) -> EvolutionChannel:
    return EvolutionChannel(
        ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1",
                      accept_groups=accept_groups))


def _upsert(**data) -> dict:
    return {"event": "messages.upsert", "data": data}


def _vote_message(selected, *, with_creation_key: bool = True, with_vote: bool = True) -> dict:
    """A ``pollUpdateMessage`` body: the creating key, and the vote with its chosen names."""
    pu: dict = {}
    if with_creation_key:
        pu["pollCreationMessageKey"] = {"remoteJid": "5511955500077@s.whatsapp.net",
                                        "fromMe": True, "id": CREATION_ID}
    if with_vote:
        vote: dict = {"senderTimestampMs": "1731900000000"}
        if selected is not None:
            vote["selectedOptions"] = selected
        pu["vote"] = vote
    return {"pollUpdateMessage": pu}


def _parse(selected, **over):
    return _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "fromMe": False, "id": VOTE_ID},
        messageType="pollUpdateMessage", message=_vote_message(selected, **over)))


# ── the read vote ──────────────────────────────────────────────────────────────────────

def test_single_choice_vote_is_read_by_name():
    msg = _parse(["Manhã"])
    assert msg.kind == MessageKind.POLL_VOTE
    assert isinstance(msg.poll_vote, PollVote)
    assert msg.poll_vote.poll_message_id == CREATION_ID
    assert msg.poll_vote.selected == ("Manhã",)
    assert msg.poll_vote.poll_id == "" and msg.poll_vote.retracted is False
    assert msg.message_id == VOTE_ID


def test_multiple_choice_vote_keeps_every_name_in_order():
    msg = _parse(["Manhã", "Noite"])
    assert msg.poll_vote.selected == ("Manhã", "Noite")


# ── the unread vote (None, never an abstention) ─────────────────────────────────────────

def test_empty_selected_options_is_unread_not_an_abstention():
    msg = _parse([])
    assert msg.kind == MessageKind.POLL_VOTE
    assert msg.poll_vote.selected is None
    assert msg.poll_vote.poll_message_id == CREATION_ID


def test_missing_vote_block_is_unread():
    msg = _parse(None, with_vote=False)
    assert msg.poll_vote.selected is None
    assert msg.poll_vote.poll_message_id == CREATION_ID


def test_non_string_names_are_unread():
    # Anything that is not a non-empty string is no name — the whole vote reads unread.
    msg = _parse([None, "", 7])
    assert msg.poll_vote.selected is None


def test_missing_creation_key_leaves_poll_message_id_empty():
    msg = _parse(["Tarde"], with_creation_key=False)
    assert msg.poll_vote.poll_message_id == ""
    assert msg.poll_vote.selected == ("Tarde",)


# ── groups: the author resolves from the phone bridge, or the vote drops ─────────────────

def _group_upsert(*, participant_alt: str | None) -> dict:
    key = {"remoteJid": ROOM, "fromMe": False, "id": VOTE_ID, "participant": AUTHOR_LID,
           "addressingMode": "lid"}
    if participant_alt is not None:
        key["participantAlt"] = participant_alt
    return _upsert(key=key, messageType="pollUpdateMessage",
                   message=_vote_message(["Manhã"]))


def test_group_vote_carries_author_and_room():
    msg = _ch(accept_groups=True).parse_inbound(_group_upsert(participant_alt=AUTHOR_PHONE))
    assert msg.kind == MessageKind.POLL_VOTE
    assert msg.author == AUTHOR_PHONE and msg.conversation_id == ROOM
    assert msg.poll_vote.selected == ("Manhã",)


def test_group_vote_without_phone_participant_is_dropped():
    # Invariant (a): no phone `participantAlt` → SILENCE, exactly as every other group shape.
    assert _ch(accept_groups=True).parse_inbound(_group_upsert(participant_alt=None)) is None
    assert _ch(accept_groups=True).parse_inbound(
        _group_upsert(participant_alt=AUTHOR_LID)) is None


def test_group_vote_dropped_when_groups_off():
    assert _ch(accept_groups=False).parse_inbound(
        _group_upsert(participant_alt=AUTHOR_PHONE)) is None


def test_own_echo_is_dropped():
    out = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "fromMe": True, "id": VOTE_ID},
        messageType="pollUpdateMessage", message=_vote_message(["Manhã"])))
    assert out is None


# ── CONTROL: the branch beside it is unchanged, and no vote is a plain None ──────────────

def test_reaction_text_and_media_are_unchanged_and_carry_no_vote():
    ch = _ch()
    reaction = ch.parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "id": "RCT1"},
        message={"reactionMessage": {"text": "🔥", "key": {"id": "TGT"}}}))
    assert reaction.kind == MessageKind.REACTION and reaction.poll_vote is None
    assert reaction.reaction.emoji == "🔥"

    text = ch.parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "id": "TXT1"},
        messageType="conversation", message={"conversation": "oi"}))
    assert text.kind == MessageKind.TEXT and text.text == "oi" and text.poll_vote is None

    media = ch.parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "id": "IMG1"},
        messageType="imageMessage",
        message={"imageMessage": {"caption": "foto", "mimetype": "image/jpeg",
                                  "url": "https://mmg.example/x"}}))
    assert media.kind == MessageKind.IMAGE and media.poll_vote is None
    assert media.media == MediaRef(ref="IMG1", mime="image/jpeg", url="https://mmg.example/x")


def test_poll_vote_default_is_none_on_an_ordinary_message():
    # The reserved field is None everywhere but on a POLL_VOTE message.
    msg = _ch().parse_inbound(_upsert(
        key={"remoteJid": "5511955500077@s.whatsapp.net", "id": "TXT2"},
        messageType="conversation", message={"conversation": "tudo bem?"}))
    assert msg.poll_vote is None


@pytest.mark.parametrize("names", [["Manhã"], ["Manhã", "Tarde", "Noite"]])
def test_read_votes_never_touch_a_log(names, caplog):
    # No option name reaches a log line (the names are third-party content).
    import logging
    with caplog.at_level(logging.DEBUG, logger="cogno_gateway.evolution"):
        _parse(names)
    for name in names:
        assert name not in caplog.text
