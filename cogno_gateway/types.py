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
class Location:
    latitude: float
    longitude: float
    name: str = ""


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
