"""Telegram parses a ``poll_answer`` update into a ``POLL_VOTE`` message.

Telegram delivers a ``poll_answer`` only for a NON-ANONYMOUS poll — the kind this channel sends —
and names the poll by ``poll_id`` (never by a message id), the voter by ``user.id``, and the choice
as ``option_ids``, the zero-based indices into the poll's options in the order the voter picked
them. The shapes below reproduce that; every id and index is synthetic (a vote on a poll with three
options, chosen by indices 0/1/2).

What the twins pin:

* a well-formed vote (one chosen index) parses to ``kind=POLL_VOTE`` with ``poll_id``, the voter as
  ``sender``, and the ordered index as ``selected`` (strings); ``poll_message_id`` stays ``""`` (no
  message id is invented) and ``retracted`` is ``False``;
* several indices keep their delivered ORDER (not sorted);
* an EMPTY ``option_ids`` is a RETRACTION — ``retracted=True`` with ``selected=None`` — never an
  invented option;
* a ``poll_answer`` missing the voter (or malformed) takes the drop path (``None``), no crash;
* CONTROL: a text, a reaction and a callback still parse exactly as before, carrying no vote — the
  new branch disturbs nothing beside it, and an ordinary message keeps ``poll_vote=None``.
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
    if option_ids is not None:
        answer["option_ids"] = option_ids
    return {"poll_answer": answer}


def _parse(option_ids, **over):
    return _ch().parse_inbound(_answer(option_ids, **over))


# ── the read vote ───────────────────────────────────────────────────────────────────────

def test_single_choice_vote_carries_poll_id_voter_and_ordered_index():
    msg = _parse([0])
    assert msg.kind == MessageKind.POLL_VOTE
    assert isinstance(msg.poll_vote, PollVote)
    assert msg.poll_vote.poll_id == POLL_ID
    assert msg.sender == str(VOTER_ID)
    assert msg.poll_vote.selected == ("0",)
    assert msg.poll_vote.poll_message_id == ""  # Telegram carries no message id for a vote
    assert msg.poll_vote.retracted is False


def test_multiple_indices_keep_their_delivered_order():
    msg = _parse([2, 0])
    assert msg.poll_vote.selected == ("2", "0")  # order preserved, not sorted
    assert msg.poll_vote.retracted is False


# ── the retraction (an empty selection, a real state) ───────────────────────────────────

def test_empty_option_ids_is_a_retraction_not_an_invented_option():
    msg = _parse([])
    assert msg.kind == MessageKind.POLL_VOTE
    assert msg.poll_vote.retracted is True
    assert msg.poll_vote.selected is None
    assert msg.poll_vote.poll_id == POLL_ID


def test_missing_option_ids_reads_as_a_retraction():
    # A malformed answer with no option_ids at all is an empty selection, never an invented vote.
    msg = _parse(None)
    assert msg.poll_vote.retracted is True and msg.poll_vote.selected is None


def test_non_integer_indices_are_ignored():
    # Anything that is not a real index is no choice; what remains decides read vs retracted.
    msg = _parse([None, "x", True, 1])
    assert msg.poll_vote.selected == ("1",) and msg.poll_vote.retracted is False
    empty = _parse([None, "x"])
    assert empty.poll_vote.selected is None and empty.poll_vote.retracted is True


# ── the malformed vote → the drop path ──────────────────────────────────────────────────

def test_vote_without_a_voter_is_dropped():
    assert _parse([0], with_user=False) is None


def test_voter_without_an_id_is_dropped():
    assert _ch().parse_inbound({"poll_answer": {"poll_id": POLL_ID, "user": {}}}) is None


# ── CONTROL: the branch beside it is unchanged, and no ordinary message carries a vote ───

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
