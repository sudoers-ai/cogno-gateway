"""
cogno_gateway.options — turn a list of choices into the right outbound message.

Host sugar for the deterministic "data → buttons" path: when a turn produced a
list of options (e.g. a tool returned the available services/slots), map it to an
``OutboundMessage`` — quick-reply **buttons** when few, a **list menu** when many.
Pure presentation, no LLM, no cognition: the *decision* to offer options lives in
the host/EGO data; this just renders it. The tap comes back as an ``INTERACTIVE``
inbound — read it with :func:`user_input`.
"""

from __future__ import annotations

from typing import Iterable, Union

from cogno_gateway.types import (
    Button,
    InboundMessage,
    ListMenu,
    ListSection,
    MessageKind,
    OutboundMessage,
)

# An option can be a Button, an (id, label) pair, or a dict from a tool result.
Option = Union[Button, tuple, dict]


def _to_button(opt: Option) -> Button:
    if isinstance(opt, Button):
        return opt
    if isinstance(opt, dict):
        oid = opt.get("id") or opt.get("value") or opt.get("key") or ""
        label = opt.get("label") or opt.get("title") or opt.get("name") or str(oid)
        return Button(str(oid), str(label))
    return Button(str(opt[0]), str(opt[1]))   # (id, label) pair


def options_to_message(
    text: str,
    options: Iterable[Option],
    *,
    max_buttons: int = 3,
    list_button: str = "Opções",
    section_title: str = "Opções",
) -> OutboundMessage:
    """Build an ``OutboundMessage`` offering ``options``.

    ``≤ max_buttons`` (default 3, WhatsApp's button cap) → quick-reply buttons;
    more → a list menu. No options → a plain text message. Accepts ``Button``s,
    ``(id, label)`` pairs, or dicts (``id``/``value``/``key`` + ``label``/
    ``title``/``name``) — whatever shape your tool returned.
    """
    buttons = [_to_button(o) for o in options]
    out = OutboundMessage(text=text)
    if not buttons:
        return out
    if len(buttons) <= max_buttons:
        out.buttons = buttons
    else:
        out.list_menu = ListMenu(button=list_button,
                                 sections=[ListSection(section_title, buttons)])
    return out


def user_input(msg: InboundMessage) -> str:
    """The user's effective input for the pipeline: a tapped option's ``id`` when
    they chose from buttons/a list, otherwise the message text."""
    if msg.kind == MessageKind.INTERACTIVE and msg.selection is not None:
        return msg.selection.id
    return msg.text
