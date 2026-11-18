"""Telegram parses a ``poll_answer`` update into a ``POLL_VOTE`` message.

Telegram delivers a ``poll_answer`` only for a NON-ANONYMOUS poll — the kind this channel sends —
and names the poll by ``poll_id`` (never by a message id), the voter by ``user.id``, and the choice
as ``option_ids``, the zero-based indices into the poll's options in the order the voter picked
them. The shapes below reproduce that; every id and index is synthetic (a vote on a poll with three
options, chosen by indices 0/1/2).

The choice reads into ``PollVote.option_ids`` (indices — Telegram's shape), NOT ``PollVote.selected``
(which is WhatsApp option NAMES): the two never share a field, so an option named "1" is never
confused with the index 1. ``selected`` therefore stays ``None`` on every Telegram vote.

What the twins pin:

* a well-formed vote (one index) → ``kind=POLL_VOTE`` with ``poll_id``, the voter as ``sender``,
  and the ordered index as ``option_ids``; ``poll_message_id`` stays ``""`` (no message id
  invented), ``selected`` is ``None``, ``retracted`` is ``False``;
* several indices keep their delivered ORDER (not sorted);
* a PRESENT, EMPTY ``option_ids`` is a RETRACTION — ``retracted=True`` with ``option_ids=None``;
* an ABSENT ``option_ids`` is UNREAD, NOT a retraction; a non-index entry voids the WHOLE vote
  (never a partial read); a bool is not an index;
* a ``poll_answer`` missing the voter (or malformed) takes the drop path (``None``), no crash;
* CONTROL: a WhatsApp ``PollVote`` (0.1.14) is byte-for-byte unchanged (``option_ids`` ``None`` and
  out of its ``repr``); a text, a reaction and a callback still parse as before, carrying no vote.
"""

from __future__ import annotations

import logging

import pytest

from cogno_gateway import (
    ChannelConfig,
    MessageKind,
    PollVote,
    TelegramChannel,
)

CFG = ChannelConfig(token="BOT123", secret="sek")
POLL_ID = "quostral-poll-7f3a1c"
VOTER_ID = 900700077


def _ch() -> TelegramChannel:
    return TelegramChannel(CFG)


def _answer(option_ids, *, with_user: bool = True, poll_id: str = POLL_ID) -> dict:
    answer: dict = {"poll_id": poll_id}
    if with_user:
        answer["user"] = {"id": VOTER_ID, "is_bot": False, "first_name": "Belmira"}
    if option_ids is not _ABSENT:
        answer["option_ids"] = option_ids
    return {"poll_answer": answer}


_ABSENT = object()  # sentinel: the `option_ids` key is left out of the update entirely


def _parse(option_ids, **over):
    return _ch().parse_inbound(_answer(option_ids, **over))


# ── the read vote ───────────────────────────────────────────────────────────────────────

def test_single_choice_vote_carries_poll_id_voter_and_ordered_index():
    msg = _parse([0])
    assert msg.kind == MessageKind.POLL_VOTE
    assert isinstance(msg.poll_vote, PollVote)
    assert msg.poll_vote.poll_id == POLL_ID
    assert msg.sender == str(VOTER_ID)
    assert msg.poll_vote.option_ids == (0,)
    assert msg.poll_vote.selected is None            # Telegram carries indices, never names
    assert msg.poll_vote.poll_message_id == ""       # Telegram carries no message id for a vote
    assert msg.poll_vote.retracted is False


def test_multiple_indices_keep_their_delivered_order():
    msg = _parse([2, 0])
    assert msg.poll_vote.option_ids == (2, 0)        # order preserved, not sorted
    assert msg.poll_vote.selected is None
    assert msg.poll_vote.retracted is False


# ── the retraction (a PRESENT, EMPTY list — a real state) ───────────────────────────────

def test_present_empty_option_ids_is_a_retraction():
    msg = _parse([])
    assert msg.kind == MessageKind.POLL_VOTE
    assert msg.poll_vote.retracted is True
    assert msg.poll_vote.option_ids is None
    assert msg.poll_vote.selected is None
    assert msg.poll_vote.poll_id == POLL_ID


# ── the unread vote (never a retraction, never a partial read) ──────────────────────────

def test_absent_option_ids_is_unread_not_a_retraction():
    msg = _parse(_ABSENT)
    assert msg.poll_vote.option_ids is None
    assert msg.poll_vote.retracted is False          # absence is unread, NOT a withdrawal
    assert msg.poll_vote.selected is None


def test_non_list_option_ids_is_unread():
    msg = _parse("0,1")
    assert msg.poll_vote.option_ids is None and msg.poll_vote.retracted is False


def test_any_non_index_entry_voids_the_whole_vote():
    # One illegible entry → the WHOLE vote is unread, never a partial read.
    msg = _parse([0, "a", 1])
    assert msg.poll_vote.option_ids is None and msg.poll_vote.retracted is False
    assert msg.poll_vote.selected is None


def test_a_bool_is_not_an_index():
    msg = _parse([True])
    assert msg.poll_vote.option_ids is None and msg.poll_vote.retracted is False


# ── the malformed vote → the drop path ──────────────────────────────────────────────────

def test_vote_without_a_voter_is_dropped():
    assert _parse([0], with_user=False) is None


def test_voter_without_an_id_is_dropped():
    assert _ch().parse_inbound({"poll_answer": {"poll_id": POLL_ID, "user": {}}}) is None


# ── CONTROL: the WhatsApp PollVote is byte-equal, and no ordinary message carries a vote ─

def test_whatsapp_poll_vote_is_byte_equal_with_option_ids_absent_from_repr():
    # A 0.1.14 WhatsApp vote never sets option_ids: it defaults to None and stays out of the repr,
    # so the string a log line or a failing assertion shows is exactly as before the field existed.
    whatsapp = PollVote(poll_message_id="3EB0QUOSTRAL0001", selected=("Manhã",))
    assert whatsapp.option_ids is None
    assert "option_ids" not in repr(whatsapp)
    assert repr(whatsapp) == (
        "PollVote(poll_message_id='3EB0QUOSTRAL0001', poll_id='', "
        "selected=('Manhã',), retracted=False)")


def test_text_reaction_and_callback_are_unchanged_and_carry_no_vote():
    ch = _ch()
    text = ch.parse_inbound({"message": {"chat": {"id": 42}, "message_id": 7, "text": "oi"}})
    assert text.kind == MessageKind.TEXT and text.poll_vote is None

    reaction = ch.parse_inbound({"message_reaction": {
        "chat": {"id": 5, "type": "private"}, "message_id": 99,
        "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
    assert reaction.kind == MessageKind.REACTION and reaction.poll_vote is None

    callback = ch.parse_inbound({"callback_query": {
        "message": {"chat": {"id": 8, "type": "private"}, "message_id": 3}, "data": "pick:a"}})
    assert callback.kind == MessageKind.INTERACTIVE and callback.poll_vote is None


def test_poll_vote_default_is_none_on_an_ordinary_message():
    msg = _ch().parse_inbound({"message": {"chat": {"id": 1}, "message_id": 2, "text": "tudo bem?"}})
    assert msg.poll_vote is None


@pytest.mark.parametrize("option_ids", [[0], [0, 2, 1], []])
def test_votes_never_touch_a_log(option_ids, caplog):
    # No poll id or option index reaches a log line at parse (the branch mirrors the others).
    with caplog.at_level(logging.DEBUG, logger="cogno_gateway.telegram"):
        _parse(option_ids)
    assert POLL_ID not in caplog.text
    for i in option_ids:
        assert str(i) not in caplog.text
