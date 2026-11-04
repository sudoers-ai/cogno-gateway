"""A markdown TABLE reaches WhatsApp and Telegram as a LIST, not as raw pipes.

The defect: a reply carrying a markdown table (header, the ``|---|`` delimiter row, the rows) was
delivered on Telegram as what it is in the source — pipes and dashes, line after line — and
WhatsApp renders no table either. An instruction to the model not to write tables is not a
guarantee, so the conversion is here, downstream of the voice, where the channel is known (the
same reason bold is converted here). On those channels — and on web, whose widget renders no
markdown either — a table becomes its header's cells on the first line, then one «• a — b — c»
line per row, the delimiter row dropped.

The CONTROLS: text without a table is the same string; the web cell does not change; a fenced code
block with pipes is not a table; a line with a pipe in a sentence («A | B») is not a table; a
header and delimiter whose cell counts differ is not a table; ``markdown``, ``plain`` and an
unknown channel keep the table. And it reaches the wire on the three WhatsApp/Telegram adapters
and through web's ``serialize``. Invented values only.
"""

import pytest

from cogno_gateway import (ChannelConfig, EvolutionChannel, OutboundMessage, TelegramChannel,
                           WebChannel, WhatsAppCloudChannel, to_channel_markup)
from cogno_gateway import markup as _markup
from tests.conftest import FakeResponse, body_of


def tables_to_lists(text):
    """The function under test, read off the module, so this file also RUNS on the tree before it
    existed (its twins red there, not a collection error)."""
    return _markup.tables_to_lists(text)

#: The SHAPE of the reported reply: a list of change requests, four columns, a bold cell.
TABLE_REPLY = """Estes são os pedidos abertos:

| Pedido | Estado | Título | Data |
|---|:---:|---|--:|
| 1001 | queued | corrigir a **frase** de boas-vindas | 03/11 |
| 1002 | merged | regra do horário | 02/11 |
| 1003 | awaiting_review | texto do lembrete | 01/11 |

Quer que eu detalhe algum?"""

AS_LIST = """Estes são os pedidos abertos:

Pedido — Estado — Título — Data
• 1001 — queued — corrigir a **frase** de boas-vindas — 03/11
• 1002 — merged — regra do horário — 02/11
• 1003 — awaiting_review — texto do lembrete — 01/11

Quer que eu detalhe algum?"""


# ── the conversion ───────────────────────────────────────────────────────────────────────

def test_GEMEO_a_table_becomes_a_list_with_the_header_first():
    assert tables_to_lists(TABLE_REPLY) == AS_LIST


@pytest.mark.parametrize("channel,bold", [("telegram", "frase"), ("whatsapp", "*frase*"),
                                          ("web", "frase")])
def test_GEMEO_on_telegram_and_whatsapp_the_table_is_a_list_and_bold_still_converts(channel, bold):
    out = to_channel_markup(TABLE_REPLY, channel)
    assert "|" not in out and "---" not in out, out
    assert out == AS_LIST.replace("**frase**", bold)


def test_a_three_column_table_without_outer_pipes():
    text = "Disciplina | Horas | Valor\n--- | --- | ---\nMatemática | 10 | R$ 100,00\nFísica | 8 | R$ 80,00"
    assert tables_to_lists(text) == ("Disciplina — Horas — Valor\n"
                                     "• Matemática — 10 — R$ 100,00\n• Física — 8 — R$ 80,00")


def test_converting_twice_equals_converting_once():
    once = to_channel_markup(TABLE_REPLY, "telegram")
    assert to_channel_markup(once, "telegram") == once


# ── the controls ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Bom dia! A aula de amanhã é às 9h.",
    "Escolha: A | B numa frase, e mais nada.",                    # a pipe in a sentence
    "a | b\nc | d",                                               # piped lines, no delimiter row
    "| a | b |\n|---|\n| 1 | 2 |",                                # delimiter with another width
    "Prazo: 2-3 dias | valor a-combinar\nentrega - retirada | ok",  # dashes, not a delimiter row
    "Escolha: A | B — ou C-D, mais nada.",                         # a pipe and a dash in a sentence
    "Totais:\n- item 1\n- item 2\n---\nfim",                      # a rule, not a table
    "",
], ids=["plain", "pipe_in_a_sentence", "no_delimiter", "width_differs", "dashes_in_cells",
        "pipe_and_dash_in_a_sentence", "hr", "empty"])
def test_CONTROL_text_that_is_not_a_table_is_the_same_string(text):
    assert tables_to_lists(text) is text or tables_to_lists(text) == text
    for channel in ("telegram", "whatsapp", "web"):
        assert to_channel_markup(text, channel) == text


def test_CONTROL_a_fenced_code_block_with_pipes_is_left_alone():
    code = "Exemplo:\n```\n| a | b |\n|---|---|\n| 1 | 2 |\n```\nfim"
    assert tables_to_lists(code) == code
    assert to_channel_markup(code, "telegram") == code


def test_GEMEO_on_web_the_table_is_a_list_too():
    """The web widget renders no markdown either (its bold cell strips for that reason): the same
    raw pipes, the same list. Through ``serialize``, web's only outbound door."""
    assert to_channel_markup(TABLE_REPLY, "web") == AS_LIST.replace("**frase**", "frase")
    assert WebChannel().serialize("s1", OutboundMessage(text=TABLE_REPLY))["response"] == \
        AS_LIST.replace("**frase**", "frase")


def test_CONTROL_markdown_plain_and_an_unknown_channel_keep_the_table():
    for channel in ("markdown", "plain", "", "carrier-pigeon"):
        assert "|---|" in to_channel_markup(TABLE_REPLY, channel), channel


# ── it reaches the wire ──────────────────────────────────────────────────────────────────

async def test_telegram_send_puts_the_list_on_the_wire(fake_httpx):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    await TelegramChannel(ChannelConfig(token="BOT123", max_chars=4000)).send(
        "42", OutboundMessage(text=TABLE_REPLY))
    sent = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert body_of(sent[0])["text"] == AS_LIST.replace("**frase**", "frase")


async def test_whatsapp_evolution_send_puts_the_list_on_the_wire(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="K", instance="i1",
                                        max_chars=4000))
    await ch.send("5500000000000@s.whatsapp.net", OutboundMessage(text=TABLE_REPLY))
    sent = [c for c in fake_httpx.calls if "sendText" in c["url"]]
    assert body_of(sent[0])["text"] == AS_LIST.replace("**frase**", "*frase*")


async def test_whatsapp_cloud_send_puts_the_list_on_the_wire(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "wamid.1"}]})}
    ch = WhatsAppCloudChannel(ChannelConfig(token="T", instance="PHONE_ID", max_chars=4000))
    await ch.send("5500000000000", OutboundMessage(text=TABLE_REPLY))
    sent = [c for c in fake_httpx.calls if "/messages" in c["url"]]
    assert body_of(sent[0])["text"]["body"] == AS_LIST.replace("**frase**", "*frase*")
