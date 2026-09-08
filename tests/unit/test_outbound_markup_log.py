"""The record that the outbound markup conversion happened — and what it may not carry.

**The defect this file is written against is a MEASUREMENT, not a bug.** After the per-channel
conversion landed, the question "does the ``**`` still reach the contact?" was asked of the
database, and it could not be answered in either direction. Three facts, each true on its own:
the host persists the reply BEFORE the adapter sees it; its ``event=outbound_attempted chars=``
is logged on the line above ``channel.send(...)``; the conversion lives INSIDE ``send``. Nothing
kept the post-conversion payload, so every number available was true about the column it read
and silent about the thing asked. The answer is not a better query — it is this record.

So the tests below are two properties, and both of them fail loudly rather than quietly:

* the numbers are the CONVERSION's, not a recomputation — take the converter out of an adapter
  and that adapter's twin turns red, because ``chars_out`` becomes ``chars_in``;
* the line carries LENGTHS and never the reply, which on the way out is the contact's own data.

The 544 is a real production figure (an ``outbound_attempted chars=544``); the text below is
invented to match it exactly, and names nobody and nothing.
"""

import ast
import logging
import pathlib

import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    OutboundMessage,
    TelegramChannel,
    WebChannel,
    WhatsAppCloudChannel,
)
from tests.conftest import FakeResponse, body_of

# A 544-character reply carrying exactly two bold pairs. Both halves of that sentence are load
# bearing: 544 is the length the host recorded on the turn that started this, and two pairs is
# what makes the two expected outputs differ from each other and from the input — a swap
# (WhatsApp) loses one character per delimiter and a strip (Telegram, web) loses two, so
# 544 → 540 and 544 → 536 could not be produced by the wrong dialect.
REPLY = (
    "Consegui confirmar o que você pediu. **Resumo do atendimento**\n\n"
    "- O horário da manhã continua livre e pode ser reservado hoje mesmo.\n"
    "- O da tarde já está ocupado, então deixei a lista de espera aberta.\n"
    "- Nenhuma alteração foi registrada até você responder esta mensagem.\n\n"
    "**Como seguir**: responda com a opção desejada e eu registro na hora. "
    "Se preferir outro dia, é só dizer qual que eu verifico a agenda inteira "
    "e volto com as possibilidades antes do fim do expediente de hoje. "
    "Fico no aguardo e qualquer dúvida pode perguntar por aqui mesmo."
)

CHARS_IN = 544
WHATSAPP_OUT = 540      # ``**`` → ``*``  ×2 pairs = 4 delimiters, one character each
STRIPPED_OUT = 536      # ``**`` → ``''`` ×2 pairs = 4 delimiters, two characters each


@pytest.fixture
def markup_log(caplog):
    """Capture this package's INFO records and hand back the ``outbound_markup`` ones.

    ``set_level`` is not decoration: the adapters' loggers are NOTSET, so without it the
    effective level is pytest's root WARNING and every assertion below would pass on an empty
    list — a test that measures nothing while looking green."""
    caplog.set_level(logging.INFO, logger="cogno_gateway")

    def records():
        return [r for r in caplog.records if "event=outbound_markup" in r.getMessage()]

    records.caplog = caplog          # type: ignore[attr-defined]
    return records


def _line(channel: str, chars_in: int, chars_out: int) -> str:
    return f"channel={channel} event=outbound_markup chars_in={chars_in} chars_out={chars_out}"


# ── the proof, one channel at a time ─────────────────────────────────────────
async def test_evolution_records_544_to_540(fake_httpx, markup_log):
    """WhatsApp through Evolution: the swap, and the label the rest of that module uses."""
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo.invalid/", token="K", instance="i1"))
    await ch.send("5500000000000@s.whatsapp.net", OutboundMessage(text=REPLY))

    assert [r.getMessage() for r in markup_log()] == [
        _line("whatsapp", CHARS_IN, WHATSAPP_OUT)]
    # ...and the number is the wire's, not the log's own arithmetic.
    sent = [c for c in fake_httpx.calls if "sendText" in c["url"]]
    assert sum(len(body_of(c)["text"]) for c in sent) == WHATSAPP_OUT


async def test_cloud_records_544_to_540(fake_httpx, markup_log):
    """The same channel through the official API — same dialect, different adapter, and the
    label is the only thing in the record that says which one ran (both are
    ``name = "whatsapp"``)."""
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "wamid.1"}]})}
    ch = WhatsAppCloudChannel(ChannelConfig(token="T", instance="PHONE_ID"))
    await ch.send("5500000000000", OutboundMessage(text=REPLY))

    assert [r.getMessage() for r in markup_log()] == [
        _line("whatsapp_cloud", CHARS_IN, WHATSAPP_OUT)]
    sent = [c for c in fake_httpx.calls if "/messages" in c["url"]]
    assert sum(len(body_of(c)["text"]["body"]) for c in sent) == WHATSAPP_OUT


async def test_telegram_records_544_to_536(fake_httpx, markup_log):
    """Telegram sets no ``parse_mode``, so the markers are stripped — four characters more than
    WhatsApp loses, which is what makes these two numbers a discriminating pair."""
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    await TelegramChannel(ChannelConfig(token="BOT123", secret="s")).send(
        "42", OutboundMessage(text=REPLY))

    assert [r.getMessage() for r in markup_log()] == [
        _line("telegram", CHARS_IN, STRIPPED_OUT)]
    sent = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert sum(len(body_of(c)["text"]) for c in sent) == STRIPPED_OUT


def test_web_records_544_to_536(markup_log):
    """``serialize`` and not ``send``: that dict is the web channel's only outbound door, so it
    is where both the conversion and its record live."""
    out = WebChannel(secret="s").serialize("session-1", OutboundMessage(text=REPLY))

    assert [r.getMessage() for r in markup_log()] == [_line("web", CHARS_IN, STRIPPED_OUT)]
    assert len(out["response"]) == STRIPPED_OUT


# ── the two properties the numbers alone do not pin ──────────────────────────
def test_the_line_is_written_even_when_nothing_was_converted(markup_log):
    """An equal pair is a REPLY with no bold in it. It is emitted so that a missing line keeps
    exactly one meaning — the conversion did not run — which is the distinction that made this
    record worth writing and the one a "log only when it changed" variant destroys."""
    plain = "Tudo certo por aqui, qualquer coisa é só chamar."
    WebChannel(secret="s").serialize("session-1", OutboundMessage(text=plain))

    assert [r.getMessage() for r in markup_log()] == [
        _line("web", len(plain), len(plain))]


async def test_the_record_never_carries_the_reply_itself(fake_httpx, markup_log):
    """The rule that decides what this line may hold: on the way out the text is the contact's
    own — their name, their number, the figures a tool read back. Adding it here would open a
    store of personal data in order to close a hole in observability, which is the trade this
    codebase refuses one layer up (the outbound-PII allowlist keeps digests, never values).

    Asserted twice on purpose. ``args`` pins THIS line's payload exactly — four values, two of
    them integers — so a ``text=%s`` appended to the format cannot pass. The substring sweep
    covers the whole capture, so a second line added elsewhere in the send path cannot leak what
    this one refused."""
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo.invalid/", token="K", instance="i1"))
    await ch.send("5500000000000@s.whatsapp.net", OutboundMessage(text=REPLY))

    (record,) = markup_log()
    assert record.args == ("whatsapp", "outbound_markup", CHARS_IN, WHATSAPP_OUT)

    captured = markup_log.caplog.text
    for fragment in ("Resumo do atendimento", "lista de espera", "Como seguir",
                     "Fico no aguardo", "horário da manhã"):
        assert fragment not in captured, f"the reply leaked into a log line: {fragment!r}"


# ── what it costs, stated as a number ────────────────────────────────────────
async def test_one_line_per_message_and_not_per_chunk(fake_httpx, markup_log):
    """The denominator, pinned rather than promised.

    The conversion runs once, before the chunker, so its record is one line per outbound
    message however many provider calls that message becomes. A reply long enough to be split
    into several chunks is the case that tells the two costs apart — placing the line inside the
    send loop would be invisible on every short reply and would multiply on exactly the long
    ones. The assertion is on the whole INFO capture, not only on the markup event, so an
    unrelated per-chunk INFO added later also lands here."""
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo.invalid/", token="K",
                                        instance="i1", max_chars=80))
    markup_log.caplog.clear()    # the count is per SEND; construction warns once per channel
    await ch.send("5500000000000@s.whatsapp.net", OutboundMessage(text=REPLY))

    assert len([c for c in fake_httpx.calls if "sendText" in c["url"]]) > 1
    assert len(markup_log()) == 1
    assert len([r for r in markup_log.caplog.records
                if r.name.startswith("cogno_gateway")]) == 1


# ── and the rule that outlives the four adapters of today ────────────────────
def test_every_function_that_converts_also_records_it():
    """Derived from the package, in the mould of the guard that pins the conversion itself, and
    checked in BOTH directions.

    A fifth channel is exactly the one nobody would remember to wire, and it would ship with the
    blind spot this record exists to close while the suite stayed green. The unit is the
    FUNCTION and not the file: a module that converts in two places and logs in one would pass a
    file-level check, and reads as covered.

    The reverse direction is not symmetry for its own sake — it is the hole this pair opened
    next door. ``test_markup.py``'s line-level rule ("no read of ``message.text`` escapes the
    converter") now admits a line that hands the text to the RECORDER, because the recorder
    provably emits only lengths. That exemption would let an adapter record without converting
    and stay green there; here it does not."""
    package = pathlib.Path(__file__).resolve().parents[2] / "cogno_gateway"
    unpaired = []
    for path in sorted(package.glob("*.py")):
        if path.name == "markup.py":
            continue                      # where both live; it converts nothing of its own
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {c.func.id for c in ast.walk(node)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            converts = "to_channel_markup" in called
            records = "log_outbound_markup" in called
            if converts != records:
                missing = "log_outbound_markup" if converts else "to_channel_markup"
                unpaired.append(f"{path.name}::{node.name} (no {missing})")

    assert not unpaired, ("converting and recording the outbound markup are one pair, and this "
                          "function has half of it: " + "; ".join(unpaired))


def test_the_guard_above_has_a_subject():
    """Its negative half. The scan asserts an absence, and an absence is also what a scan that
    found nothing at all reports — a renamed converter would empty the list and read as green."""
    package = pathlib.Path(__file__).resolve().parents[2] / "cogno_gateway"
    converting = [p.name for p in sorted(package.glob("*.py"))
                  if p.name != "markup.py" and "to_channel_markup(" in p.read_text(encoding="utf-8")]
    assert sorted(converting) == ["cloud.py", "evolution.py", "telegram.py", "web.py"]
