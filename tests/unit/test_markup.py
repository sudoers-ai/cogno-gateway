"""Unit tests for the outbound markup adapter.

Two halves, and the second is the one that matters. The first pins the conversion: a paired
``**`` becomes the channel's own bold. The second pins that the conversion actually REACHES the
provider payload on every channel — the previous attempt at this defect lived one layer up, was
green, and changed nothing for anybody, so a converter nobody calls is the failure mode this
file is written against.
"""

import pytest

from cogno_gateway import (
    Button,
    ChannelConfig,
    EvolutionChannel,
    ListMenu,
    ListSection,
    OutboundMessage,
    TelegramChannel,
    WebChannel,
    WhatsAppCloudChannel,
    to_channel_markup,
)
from cogno_gateway.markup import _BOLD_MARK
from tests.conftest import FakeResponse, body_of


# ── the conversion ───────────────────────────────────────────────────────────
def test_whatsapp_bold_is_one_asterisk():
    """The live defect: the contact was reading ``**Setembro de 2026**`` with the pair showing."""
    assert to_channel_markup("**Setembro de 2026**", "whatsapp") == "*Setembro de 2026*"


def test_whatsapp_bold_already_converted_is_untouched():
    """Idempotence, the direct form: the output of a conversion survives another one.

    Someone will eventually call this twice — a wrapper, a retry, a second adapter layer — and
    the second call has to be a no-op rather than a corruption."""
    once = to_channel_markup("**Setembro**", "whatsapp")
    assert once == "*Setembro*"
    assert to_channel_markup(once, "whatsapp") == "*Setembro*"


@pytest.mark.parametrize("channel", sorted(_BOLD_MARK) + ["carrier-pigeon", ""])
def test_converting_twice_equals_converting_once(channel):
    """Idempotence over every cell of the table at once, including the unknown branch.

    Written as a property rather than a case list because the table is the thing that will grow,
    and a new channel added without this guarantee is exactly the one nobody would re-check."""
    text = "**Total**: R$ 1.250,00 — **hoje** e depois **amanhã**"
    once = to_channel_markup(text, channel)
    assert to_channel_markup(once, channel) == once


def test_arithmetic_is_not_markup():
    """THE negative twin. A converter generous enough to see bold here would eat a contact's
    own arithmetic out of their reply.

    Two shapes, and they fail the match for different reasons, which is why both are here: the
    single asterisks never form a ``**`` at all, while the doubled ones ARE a pair and are
    rejected only by the flanking rule (an opening delimiter must be followed by a non-space)."""
    for channel in ("whatsapp", "telegram", "web", "carrier-pigeon"):
        assert to_channel_markup("2 * 3 * 4", channel) == "2 * 3 * 4"
        assert to_channel_markup("2 ** 3 ** 4", channel) == "2 ** 3 ** 4"


def test_other_loose_asterisks_survive():
    """The same boundary on the shapes that actually turn up in a reply: a bullet list, a
    footnote marker, a glob, a shell path. None of them is a left-flanking pair."""
    for text in (
        "* item um\n* item dois",
        "Preço final *\n* sujeito a confirmação",
        "guarde em /var/log/*.log",
        "3 * 4 = 12",
    ):
        assert to_channel_markup(text, "whatsapp") == text


def test_unpaired_double_asterisk_is_left_exactly_as_it_is():
    """The decision on a lone ``**``: leave it.

    It is not bold — there is no closing half — so there is nothing to convert. Dropping it
    would delete characters the writer may have meant, and halving it to ``*`` would be worse
    than either: on WhatsApp that survivor can pair with an unrelated asterisk further down the
    message and invent bold nobody asked for. Leaving it is the only choice that cannot
    fabricate formatting."""
    assert to_channel_markup("Vou verificar **isso", "whatsapp") == "Vou verificar **isso"
    assert to_channel_markup("**", "whatsapp") == "**"
    assert to_channel_markup("****", "whatsapp") == "****"
    assert to_channel_markup("fecha aqui** e mais nada", "telegram") == "fecha aqui** e mais nada"


def test_telegram_gets_the_markers_removed_and_only_that():
    """The dialect chosen for Telegram, and the reason it is neither of the two markup ones.

    This gateway sets no ``parse_mode``, so Telegram renders the body literally and any marker
    is visible punctuation. Enabling a parse mode would make the WHOLE body a parse surface,
    which forces escaping every other special character — and escaping is not idempotent, which
    is a property this module is required to keep. So: strip, and nothing else moves."""
    assert to_channel_markup("**Setembro de 2026**", "telegram") == "Setembro de 2026"
    assert to_channel_markup("**a** e **b**", "telegram") == "a e b"
    # only the markers: no escaping, no entity, no tag, nothing else rewritten
    assert to_channel_markup("R$ 5 < R$ 10 & **pronto**", "telegram") == "R$ 5 < R$ 10 & pronto"


def test_web_markdown_is_left_intact():
    assert to_channel_markup("**Setembro**", "web") == "**Setembro**"


def test_unknown_channel_falls_to_plain_text_without_raising():
    """A transport nobody taught this table about still gets a readable answer. A converter
    that raised on an unknown channel would take the contact's whole reply down over its
    formatting, which is the least important thing in the message."""
    assert to_channel_markup("**Setembro**", "carrier-pigeon") == "Setembro"
    assert to_channel_markup("**Setembro**", "") == "Setembro"
    assert to_channel_markup("**Setembro**", "  WHATSAPP  ") == "*Setembro*"   # case/space tolerant
    assert to_channel_markup("", "carrier-pigeon") == ""


def test_a_bold_run_never_crosses_a_line():
    """A stray ``**`` on one line must not reach forward and swallow everything down to another
    one. Bold that spans a line break is left alone — the conservative half of the trade."""
    text = "**Setembro\nde 2026**"
    assert to_channel_markup(text, "whatsapp") == text
    assert to_channel_markup("**a**\n**b**", "whatsapp") == "*a*\n*b*"


def test_only_the_markers_change():
    """Everything that is not a bold delimiter survives byte for byte — the property the live
    trace showed the voicer breaking, stated as an assertion."""
    original = "📅 **Setembro de 2026**\n\n• 03/09 · 14h · consulta\n• 10/09 · 09h · retorno\n\nR$ 1.250,00"
    converted = to_channel_markup(original, "whatsapp")
    assert converted == original.replace("**", "*")
    assert converted.replace("*", "") == original.replace("*", "")


# ── the table, against the one in cogno-praxis ───────────────────────────────
def test_the_bold_dialect_agrees_with_the_praxis_table():
    """A DUPLICATED CONTRACT, pinned in the mould praxis itself uses for ``money_brl``.

    ``cogno_praxis.render._BOLD`` holds the same fact for the emitting side — what mark a tool
    WRITES — and this table holds it for the converting side. They cannot be imported into each
    other (different repos, no dependency either way), so the literals are pinned here and the
    divergence is DECLARED rather than left to be discovered on a phone.

    Where they agree, they must keep agreeing. Where they differ, there is exactly one cell,
    and this test is what makes flipping it a deliberate act."""
    praxis_bold = {           # cogno_praxis/render.py::_BOLD, read at 2c8c525
        "whatsapp": "*",
        "telegram": "",
        "web": "",
        "markdown": "**",
        "plain": "",
    }
    agreeing = ["whatsapp", "telegram", "markdown", "plain"]
    assert {k: _BOLD_MARK[k] for k in agreeing} == {k: praxis_bold[k] for k in agreeing}

    # The one declared divergence. praxis emits nothing for web; this table leaves the voicer's
    # markdown alone there. Both were written knowing the widget renders `whitespace-pre-wrap`
    # with no markdown library, so on today's surface the pair stays visible on web — see the
    # table's comment. Whoever owns that surface decides; until then the difference is pinned.
    assert _BOLD_MARK["web"] == "**" != praxis_bold["web"]


# ── it reaches the wire ──────────────────────────────────────────────────────
async def test_whatsapp_evolution_send_puts_one_asterisk_on_the_wire(fake_httpx):
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="K", instance="i1"))
    await ch.send("5511999999999@s.whatsapp.net", OutboundMessage(text="**Setembro de 2026**"))
    sent = [c for c in fake_httpx.calls if "sendText" in c["url"]]
    assert body_of(sent[0])["text"] == "*Setembro de 2026*"


async def test_whatsapp_cloud_send_puts_one_asterisk_on_the_wire(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "wamid.1"}]})}
    ch = WhatsAppCloudChannel(ChannelConfig(token="T", instance="PHONE_ID"))
    await ch.send("5511999999999", OutboundMessage(text="**Setembro de 2026**"))
    sent = [c for c in fake_httpx.calls if "/messages" in c["url"]]
    assert body_of(sent[0])["text"]["body"] == "*Setembro de 2026*"


async def test_telegram_send_puts_the_stripped_text_on_the_wire(fake_httpx):
    fake_httpx.routes = {"sendMessage": FakeResponse({"result": {"message_id": 1}})}
    await TelegramChannel(ChannelConfig(token="BOT123")).send(
        "42", OutboundMessage(text="**Setembro de 2026**"))
    sent = [c for c in fake_httpx.calls if "sendMessage" in c["url"]]
    assert body_of(sent[0])["text"] == "Setembro de 2026"
    # and no parse_mode was invented along the way — the strip is the whole change
    assert "parse_mode" not in body_of(sent[0])


def test_web_serialize_leaves_the_markdown_alone():
    out = WebChannel().serialize("s1", OutboundMessage(text="**Setembro de 2026**"))
    assert out["response"] == "**Setembro de 2026**"


async def test_the_menu_and_button_bodies_are_converted_too(fake_httpx):
    """The two branches that never reach the chunker. A conversion placed inside the chunk loop
    would be green on the common path and silently leave these two behind."""
    fake_httpx.routes = {
        "sendList": FakeResponse({"key": {"id": "m1"}}),
        "sendButtons": FakeResponse({"key": {"id": "m2"}}),
    }
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="K", instance="i1"))
    menu = ListMenu(sections=[ListSection("S", [Button("a", "A")])])
    await ch.send("55119@s.whatsapp.net", OutboundMessage(text="**Escolha**:", list_menu=menu))
    assert body_of([c for c in fake_httpx.calls if "sendList" in c["url"]][0])[
        "description"] == "*Escolha*:"

    await ch.send("55119@s.whatsapp.net",
                  OutboundMessage(text="**Confirma?**", buttons=[Button("y", "Sim")]))
    assert body_of([c for c in fake_httpx.calls if "sendButtons" in c["url"]][0])[
        "description"] == "*Confirma?*"


async def test_the_conversion_happens_before_the_chunker(fake_httpx):
    """Order, pinned where it is decided.

    The chunker is the last thing to touch the text, and it splits on whitespace — it reads no
    markup. Converting first means the pair is still intact when it is matched; converting after
    would hand each chunk a half-pair that no rule here may touch. Every chunk of a long bold
    reply must therefore carry the single asterisk, never the double one."""
    fake_httpx.routes = {"sendText": FakeResponse({"key": {"id": "m1"}})}
    ch = EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="K", instance="i1",
                                        max_chars=80))
    body = "\n\n".join(f"**Bloco {i}** com algum texto de enchimento para forçar a divisão."
                       for i in range(4))
    await ch.send("55119@s.whatsapp.net", OutboundMessage(text=body))
    chunks = [body_of(c)["text"] for c in fake_httpx.calls if "sendText" in c["url"]]
    assert len(chunks) > 1
    assert all("**" not in c for c in chunks)
    assert sum(c.count("*Bloco") for c in chunks) == 4
