"""
cogno_gateway.types — normalized, channel-agnostic message model.

Every channel adapter parses its provider payload into an ``InboundMessage`` and
renders an ``OutboundMessage`` back to its provider. Messages are **typed by
content** (``MessageKind``) so a host handles text, media, reactions, location,
… uniformly across Telegram / WhatsApp / Web.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    LOCATION = "location"
    CONTACT = "contact"
    REACTION = "reaction"
    STICKER = "sticker"
    INTERACTIVE = "interactive"   # a quick-reply / list / inline-button selection
    UNKNOWN = "unknown"


@dataclass
class MediaRef:
    """A reference to a media item — a provider file id or a URL. The bytes are
    fetched lazily via ``Channel.fetch_media`` (e.g. to hand audio to cogno-vox)."""

    ref: str = ""          # provider file_id / media key (empty for outbound-by-URL)
    mime: str = ""
    caption: str = ""
    filename: str = ""
    url: str = ""          # direct URL when the provider gives one (else via fetch_media)


@dataclass
class Reaction:
    """An emoji reaction to a previous message (inbound: a user reacted; outbound:
    react to the user's message)."""

    emoji: str
    target_message_id: str


@dataclass
class Button:
    """A quick-reply button to offer (outbound). ``id`` is the stable payload the
    provider echoes back when tapped; ``title`` is the visible label."""

    id: str
    title: str


@dataclass
class ButtonReply:
    """The user's tap on a button / list option (inbound). ``id`` is the payload
    you sent; ``title`` is what they saw."""

    id: str
    title: str = ""


@dataclass
class ListSection:
    """A titled group of options inside a list menu (rows reuse ``Button``)."""

    title: str
    rows: list["Button"] = field(default_factory=list)


@dataclass
class ListMenu:
    """A list/menu of options (outbound) — for >3 choices, where quick-reply
    buttons don't fit (WhatsApp caps buttons at 3; a list holds up to ~10). The
    body text comes from ``OutboundMessage.text``; ``button`` is the label that
    opens the menu."""

    button: str = "Opções"
    sections: list[ListSection] = field(default_factory=list)


@dataclass
class Location:
    latitude: float
    longitude: float
    name: str = ""


@dataclass
class Template:
    """A pre-approved provider template (e.g. a WhatsApp Cloud API ``utility``
    template for a proactive reminder sent outside the 24h service window). The
    ``params`` fill the template's body placeholders in order."""

    name: str
    lang: str = "pt_BR"
    params: list[str] = field(default_factory=list)


@dataclass
class InboundMessage:
    """A message received from a channel, normalized."""

    channel: str                       # "telegram" | "whatsapp" | "web"
    sender: str                        # chat id / remoteJid / web session id
    kind: MessageKind = MessageKind.TEXT
    message_id: str = ""
    text: str = ""
    media: Optional[MediaRef] = None
    reaction: Optional[Reaction] = None
    location: Optional[Location] = None
    selection: Optional[ButtonReply] = None   # a tapped quick-reply / list option
    reply_to: str = ""                 # quoted/replied-to text, if any
    raw: dict = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """A reply to send back. ``audio`` carries voice-note bytes (e.g. from a
    cogno-vox TTS); ``media`` carries documents/images to attach."""

    text: str = ""
    audio: Optional[bytes] = None
    audio_format: str = "opus"
    media: list[MediaRef] = field(default_factory=list)
    reaction: Optional[Reaction] = None
    buttons: list["Button"] = field(default_factory=list)   # quick-replies (≤3) under the text
    list_menu: Optional["ListMenu"] = None  # a menu of >3 options (WhatsApp list / TG keyboard)
    template: Optional["Template"] = None   # proactive send outside the 24h window


@dataclass
class ChannelConfig:
    """Per-tenant channel credentials/settings — host-injected (never from a DB
    inside the lib). Adapters read the fields they need."""

    token: str = ""        # telegram bot token / evolution api key
    base_url: str = ""     # evolution instance url (provider API base)
    instance: str = ""     # evolution instance name
    secret: str = ""       # webhook verification secret
    max_chars: int = 0     # outbound chunk size (0 → adapter default)
    timeout: float = 15.0
    extra: dict = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str = ""
