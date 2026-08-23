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


# The presence states Baileys/WhatsApp reports. Only two of them say anything about whether
# input is still being produced; ``available``/``unavailable`` are online/offline and say
# nothing at all, which is why they are named here but excluded from both predicates below.
PRESENCE_PRODUCING = ("composing", "recording")
PRESENCE_IDLE = ("paused",)


@dataclass
class PresenceEvent:
    """The other side is producing input — or stopped.

    **Not a message.** It has no ``message_id``, is never deduplicated, never becomes a turn
    and never reaches the pipeline. Its only power is to move a deadline: it can DELAY a turn
    (someone is still typing) or BRING IT FORWARD (they stopped). It can never be the reason a
    turn happens, and never the reason one does not.

    That asymmetry is deliberate, because the signal is best-effort in two independent ways.
    Only Baileys-backed WhatsApp reports it at all — the Telegram Bot API has no update type
    for a user typing, and neither does WhatsApp Cloud — and even there it arrives only if the
    contact has not turned off "last seen and online" in their privacy settings. So **absence
    of presence means "no information", never "they stopped"**: a reader that treats silence
    as idle would fire early for every privacy-conscious contact.

    ``recording`` counts as producing alongside ``composing``: a voice note takes longer to
    make than a sentence takes to type, and dropping it would fire the turn in the middle of
    exactly the input that needs the most patience.
    """

    channel: str
    sender: str
    state: str                     # composing | recording | paused | available | unavailable

    @property
    def is_producing(self) -> bool:
        """Still typing or recording → a turn waiting on this sender should hold."""
        return self.state in PRESENCE_PRODUCING

    @property
    def is_idle(self) -> bool:
        """Stopped producing → a turn waiting on this sender may go NOW."""
        return self.state in PRESENCE_IDLE


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
    sender_user_id: str = ""           # provider stable user id when distinct from sender —
    #                                    WhatsApp Cloud BSUID (business-scoped user id): with the
    #                                    2026 usernames rollout a user may hide their phone, so
    #                                    `from`/`wa_id` become conditional and this is the stable
    #                                    identity key (sender falls back to it when the phone is
    #                                    absent). Empty on channels without the concept.
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
    # When True, verify() FAILS CLOSED if no secret is configured (an inbound webhook is rejected
    # rather than trusted). Default False keeps the dev/demo behaviour (open with a warning); the
    # host sets it True in production so a signature-capable channel cannot run unverified.
    require_secret: bool = False
    max_chars: int = 0     # outbound chunk size (0 → adapter default)
    timeout: float = 15.0
    extra: dict = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str = ""
