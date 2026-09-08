"""
cogno_gateway.markup — the one place a channel's **bold dialect** is decided.

WhatsApp's bold is a SINGLE asterisk. Markdown's is a double one. Nothing between the
voicer and the contact converted between them, so a reply reached the phone reading
``**Setembro de 2026**``, asterisks and all.

**Why the fix lives here and not in the vertical that wrote the text.** It was tried there
first (``cogno-praxis`` #121, which flipped its emitter to a single asterisk) and the
measurement, taken on the same scenario on both sides of that merge, was:

    before  the tool emitted  **   →  delivered to the contact: `**`
    after   the tool emits     *   →  delivered to the contact: `**`

— because the LOCUTOR rewrites the marker. Compared field by field on the turn trace, the
executor's draft carried ONE asterisk and the delivered reply carried TWO, with every other
byte (lines, dates, separators, order) identical; it does this even when the draft carries no
asterisk at all. So **any formatting decision taken upstream of the voicer is a suggestion**.
This module is downstream of it, and it is the only layer that knows which channel the text is
going to — the channel does not reach a tool (there is no per-call channel signal in the MCP
contract), which is the same reason ``cogno_praxis.render`` has to read an environment
variable to guess.

**The division of labour, stated once:** the vertical decides STRUCTURE (which fields, which
order, which grouping); the gateway decides MARKUP. A side effect of that split is the reason
this is worth building: once the gateway converts, whatever marker the vertical emitted stops
mattering, because it is corrected on the way out.

**What this is NOT.** It is not a markdown parser and must never become one. The target is one
shape — a paired ``**`` bold run — and the rule for recognising it is CommonMark's own
left/right-flanking rule (the opening ``**`` must be followed by a non-space, the closing one
preceded by a non-space), narrowed further to a single line. That narrowing is what keeps
``2 ** 3 ** 4`` intact: its ``**`` is followed by a space, so it is not an opening delimiter —
in CommonMark either, which is the boundary worth holding. A converter more generous than the
spec it imitates would eat arithmetic, glob patterns and file paths out of a contact's reply.

**Unpaired ``**`` is left exactly as it is.** A lone double asterisk is not bold: it has no
closing half, so nothing about it says "emphasis". Deleting it would remove characters the
writer may have meant; halving it to ``*`` would be worse, because on WhatsApp that single
asterisk can pair with an unrelated one later in the message and invent bold that nobody asked
for. Leaving it is the only choice that cannot fabricate formatting.

**Idempotent by construction.** Converting twice cannot damage the text: the WhatsApp output
``*x*`` no longer contains a ``**`` to match, the stripped output contains no marker at all,
and the markdown cell is the identity. This matters because a second caller will eventually
appear, and the second call must be a no-op rather than a corruption.
"""

from __future__ import annotations

import re

__all__ = ["to_channel_markup"]


#: The bold delimiter to WRITE, per channel — the one table this module exists to own.
#:
#: It is deliberately the mirror of ``cogno_praxis.render._BOLD``, which holds the same fact for
#: the emitting side (what a tool WRITES); this one holds it for the converting side (what a
#: ``**`` arriving from the voicer is TURNED INTO). The two answer different questions about the
#: same fact — "what does bold look like on this channel" — so they must not disagree, and where
#: they do, the divergence is declared rather than discovered:
#:
#: * ``whatsapp`` → ``*``. Agrees with praxis. This is the live defect this module exists for.
#: * ``telegram`` → strip. Agrees with praxis (which holds ``""``), and for the same reason:
#:   this gateway sets no ``parse_mode`` on ``sendMessage`` (grep the package — there is none),
#:   so Telegram renders the body literally and every marker is just visible punctuation. See
#:   the note below on what it would take to change that, and why it is not free.
#: * ``web`` → left as markdown. **This is the one cell that diverges from praxis**, which holds
#:   ``""`` for web on the stated ground that the widget renders no markup. That ground checks
#:   out — the chat surface that consumes this channel's ``{session_id, response}`` payload
#:   renders the assistant bubble as ``whitespace-pre-wrap``, and neither UI checkout depends on
#:   a markdown renderer — so on today's widget a ``**`` reaches the contact visible here too.
#:   The cell stays ``**`` because that is the instruction this module was built to, and because
#:   flipping it is a decision about a surface in another repo that should be taken by whoever
#:   owns that surface. It is one line, and this comment is the evidence for taking it.
#: * ``markdown``/``plain`` are not gateway channel names. They are here so praxis's channel
#:   vocabulary maps onto this table one-for-one instead of falling through the unknown branch.
_BOLD_MARK = {
    "whatsapp": "*",
    "telegram": "",
    "web": "**",
    "markdown": "**",
    "plain": "",
}

#: What an unrecognised channel gets. Plain text is readable on every transport, and a converter
#: that raised because a new channel arrived would take the contact's answer down with it — the
#: formatting is the least important thing in the message.
_UNKNOWN_MARK = ""

#: The markdown source marker this module converts FROM.
_MD_BOLD = "**"

#: A paired ``**`` bold run, by CommonMark's flanking rule, on a single line.
#:
#: ``(?=\S)`` — an opening ``**`` is followed by a non-space, which is what rejects the ``**``
#: in ``2 ** 3 ** 4``. ``(?<=\S)`` — a closing ``**`` is preceded by a non-space. ``[^\n]+?`` —
#: the content is non-empty (so ``****`` matches nothing) and never crosses a line, so a stray
#: ``**`` in one paragraph cannot reach forward and bold everything down to another one.
_BOLD_RUN = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")


def to_channel_markup(text: str, channel: str = "") -> str:
    """Rewrite markdown bold into ``channel``'s dialect. Everything else is left alone.

    ``**Setembro**`` → ``*Setembro*`` on WhatsApp, ``Setembro`` on Telegram, unchanged on web.
    An unknown or empty channel falls to plain text — never an exception.

    An EMPTY channel resolving to plain is the second declared divergence from praxis, whose
    ``resolve_channel`` sends it to WhatsApp instead. That is the right answer THERE and the
    wrong one here, and for one reason: praxis cannot know the channel, so its default is a
    considered guess about the deployment; every caller in this library passes the adapter's own
    ``name``, so an empty channel is not a deployment fact but a caller that passed nothing, and
    guessing WhatsApp for it would write a marker on a transport we were not told about.

    Applied to the text on its way into a provider payload, so it is the last thing that
    happens to the reply inside this library.
    """
    body = text if isinstance(text, str) else str(text or "")
    if not body or _MD_BOLD not in body:
        return body

    mark = _BOLD_MARK.get(str(channel or "").strip().lower(), _UNKNOWN_MARK)
    if mark == _MD_BOLD:
        # The identity cell. Returned unconverted rather than substituted with itself, so
        # "the gateway does not touch this channel's text" is a property of the code and not
        # an arithmetic accident of the table.
        return body

    return _BOLD_RUN.sub(lambda m: f"{mark}{m.group(1)}{mark}", body)


# ── What it would take to give Telegram real bold ────────────────────────────────────────────
#
# Not a table edit. A Telegram ``parse_mode`` turns the WHOLE message body into a parse surface:
# with ``HTML`` an unescaped ``<`` in the reply, with ``Markdown`` an unbalanced ``_`` or ``[``,
# makes the Bot API reject the entire message — and the outbound retry in ``telegram.py`` is a
# TRANSPORT retry, so it does not rescue a rejection. Enabling either dialect therefore means
# escaping every other special character in the body, and that is where it collides with a
# property this module is required to keep: escaping is not idempotent (a second pass escapes
# the escapes, and the ``<b>`` this module just wrote becomes ``&lt;b&gt;``). Keeping both would
# take exactly the markdown parser the module docstring forbids.
#
# So the honest options are the two ends, not a middle: strip (today, and idempotent), or a real
# parser that owns escaping and emits once. That is a decision, not an oversight.
