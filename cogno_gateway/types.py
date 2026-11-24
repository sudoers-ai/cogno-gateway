"""
cogno_gateway.types — normalized, channel-agnostic message model.

Every channel adapter parses its provider payload into an ``InboundMessage`` and
renders an ``OutboundMessage`` back to its provider. Messages are **typed by
content** (``MessageKind``) so a host handles text, media, reactions, location,
… uniformly across Telegram / WhatsApp / Web.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Optional


# The MIME types every adapter sends as a visible IMAGE (WhatsApp `sendMedia` with
# `mediatype: image`, Telegram `sendPhoto`, Cloud API `type: image`). JPEG and PNG are the formats
# all three providers document for a photo; anything else — WebP included, or no MIME at all —
# still goes as a document, exactly as before.
IMAGE_MIMES = frozenset({"image/jpeg", "image/png"})


def image_mime(mime: str) -> str:
    """The normalized MIME (lower case, parameters dropped) when ``mime`` is sent as a visible
    image; ``""`` otherwise."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    return base if base in IMAGE_MIMES else ""


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
    MEMBERSHIP = "membership"     # THIS bot joined or left a group (``InboundMessage.membership``)
    POLL_VOTE = "poll_vote"       # a vote on a poll THIS bot sent (``InboundMessage.poll_vote``)
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


@dataclass(frozen=True)
class Membership:
    """THIS bot's own membership of a group changed (0.1.6, Telegram only, opt-in).

    ``status`` is :data:`MEMBERSHIP_JOINED` or :data:`MEMBERSHIP_LEFT` — the two facts a host
    acts on, never the provider's own vocabulary (``member``/``administrator``/``restricted``/
    ``left``/``kicked``), which is folded at parse. ``title`` is the room's display name as the
    provider sent it (``""`` when absent): a host may show it to an operator, and it is the
    room's text, so it is never a log value. Nothing about WHO added or removed the bot is
    carried: a host that registers a room has no use for that person's id."""

    status: str
    title: str = ""


#: The two values of :attr:`Membership.status`.
MEMBERSHIP_JOINED = "joined"
MEMBERSHIP_LEFT = "left"


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


@dataclass(frozen=True)
class Poll:
    """A poll to offer (outbound) — a ``question`` with 2–12 named ``options``.

    ``multiple`` lets the voter pick more than one. The vote comes back BY NAME (the provider
    echoes the chosen option text, not an index), so the options must be distinct ignoring case;
    a case-only variation would make two votes indistinguishable.

    Frozen, and validated at construction. Every :class:`ValueError` it raises carries a FIXED
    message that NEVER echoes the question or an option: the text is third-party content and has
    no place in an exception string (which a host may log). Only WhatsApp via ``EvolutionChannel``
    sends it today; the other adapters refuse with ``error="poll_unsupported"`` and no HTTP."""

    question: str
    options: tuple[str, ...]
    multiple: bool = False

    def __post_init__(self) -> None:
        if not (self.question or "").strip():
            raise ValueError("Poll.question must not be empty")
        if not (2 <= len(self.options) <= 12):
            raise ValueError("Poll.options must hold between 2 and 12 entries")
        seen: set[str] = set()
        for opt in self.options:
            if not (opt or "").strip():
                raise ValueError("Poll.options entries must each be non-empty")
            key = opt.strip().casefold()
            if key in seen:
                raise ValueError("Poll.options must be unique, ignoring case")
            seen.add(key)


@dataclass(frozen=True)
class PollVote:
    """A vote on a poll THIS bot sent (inbound) — who chose what, on which poll.

    ``poll_message_id`` is the provider id of the message that CREATED the poll (the key a host
    looks the poll up by). ``poll_id`` is a provider-level poll identifier that WhatsApp does not
    carry — it is reserved for Telegram, whose ``poll_answer`` names the poll by id, not by the
    creating message — so it stays ``""`` on a WhatsApp vote.

    The choice rides in ONE of two fields, by provider — never both, so neither ever carries the
    other's meaning. ``selected`` is the chosen option NAMES (WhatsApp); ``option_ids`` is the chosen
    zero-based option INDICES (Telegram). Each is ``None`` when its provider did not deliver a
    readable choice, which means **UNREAD**, never an abstention.

    ``selected`` is the WhatsApp option NAMES, as a tuple, when the provider delivered a non-empty
    list of them; otherwise ``None``. A WhatsApp vote comes back with its names only once the
    gateway decrypts it against the creating message; an empty ``selectedOptions`` is AMBIGUOUS — a
    vote withdrawn, or a decryption that failed — and counting it as "voted for nothing" would
    invent a result, so it is ``None`` (the house rule: a missing measurement is ``None``, never a
    zero). It stays ``None`` on a Telegram vote (which carries indices, not names).

    ``option_ids`` is the TELEGRAM option indices, as a tuple of ints in the order the voter picked
    them, when ``poll_answer`` delivered a present, non-empty list in which EVERY entry is an index
    (an absent list, a non-list, or any non-index entry makes the WHOLE vote unread — ``None`` —
    never a partial read). It is ``None`` on a WhatsApp vote. Indices, not names, because Telegram
    sends indices into the poll's options and the names live with the host that created the poll.

    ``retracted`` is a Telegram-only state: an explicit withdrawal, which ``poll_answer`` reports as
    a present but EMPTY option list — distinct from unread. It stays ``False`` on a WhatsApp vote,
    where a withdrawal is indistinguishable from the unread case, and on a Telegram vote that was
    read or unread."""

    poll_message_id: str = ""
    poll_id: str = ""
    selected: Optional[tuple[str, ...]] = None
    retracted: bool = False
    # The Telegram option indices (``poll_answer.option_ids``), in the order the voter picked them;
    # ``None`` on a WhatsApp vote and on an unread Telegram vote. Additive and ``repr``-excluded, so
    # a WhatsApp ``PollVote`` (0.1.14) prints byte for byte as before the field existed.
    option_ids: Optional[tuple[int, ...]] = field(default=None, repr=False, kw_only=True)


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

    # ── Group chats: WHERE to answer vs WHO spoke. ──
    # Only Telegram fills them, and only when the host opted in (``ChannelConfig.accept_groups``,
    # since 0.1.4). Otherwise every adapter DROPS group traffic at parse (Telegram `_is_group`,
    # Evolution `@g.us`, Cloud `group_id`), and a message that reaches a host is a 1:1 message
    # whose fields keep their defaults, which ARE the 1:1 case. They are keyword-only and left
    # out of ``repr`` while at their default (see ``__repr__``), so a 1:1 message constructs,
    # compares, matches and prints exactly as before.
    #
    # In a Telegram group ``sender`` is the ROOM (the same value as ``conversation_id``), never
    # the member: Telegram numbers messages per chat, so a host that deduplicates on
    # ``(sender, message_id)`` stays correct, and a reply sent to ``sender`` goes to the room.
    conversation_id: str = field(default="", kw_only=True)
    #   where a reply goes: the group JID (`…@g.us`) or the negative Telegram chat id.
    #   "" = the sender's own 1:1 chat, i.e. reply to ``sender`` as today.
    author: str = field(default="", kw_only=True)
    #   the member who wrote it (Evolution `key.participant`, Telegram `from.id`).
    #   "" = the sender is the author, which is always true of a 1:1 message.
    mentions_bot: bool = field(default=False, kw_only=True)
    #   the text @-mentions this bot. Meaningful only with ``conversation_id`` set. Telegram fills
    #   it at parse (it knows its own ``@handle``); Evolution cannot — a WhatsApp mention is by the
    #   bot's own LID, which the pure parser does not know — so there it stays False and the HOST
    #   decides it from ``mentioned_ids`` against the room's stored ``bot_lid``.
    mentioned_ids: tuple[str, ...] = field(default=(), kw_only=True)
    #   the LIDs (``<digits>@lid``) this message @-mentions, verbatim from Evolution
    #   ``data.contextInfo.mentionedJid`` (a WhatsApp mention is by LID, not by phone). A host
    #   matches them against the bot's own LID to decide ``mentions_bot`` itself, which the pure
    #   parser cannot. ``()`` = the message mentions nobody. Meaningful only in a group; reserved
    #   like the fields above (hidden from ``repr`` at its default, see ``_GROUP_FIELDS``).
    reply_to_bot: bool = field(default=False, kw_only=True)
    #   the message replies to one of this bot's own messages. Meaningful only in a group.
    #   Telegram fills it at parse (it carries the quoted message's author); Evolution cannot —
    #   its parser is pure and the quote carries only an id — so there it stays False and the HOST
    #   decides it from ``reply_to_message_id`` against the room's buffer.
    reply_to_message_id: str = field(default="", kw_only=True)
    #   the provider id of the message this one QUOTES (Evolution `data.contextInfo.stanzaId`),
    #   exposed so a host can decide ``reply_to_bot`` itself: the room's buffer already holds the
    #   bot's own sends under `bot:<id>`, so a reply whose quoted id is one of them called the bot.
    #   "" = this message quotes nothing. Meaningful only in a group; the pure parser never acts on
    #   it. Reserved like the fields above: hidden from ``repr`` at its default (see ``_GROUP_FIELDS``).
    author_is_bot: bool = field(default=False, kw_only=True)
    #   the author is a bot (Telegram `from.is_bot`). A 1:1 chat with a bot never reaches us.
    membership: Optional[Membership] = field(default=None, kw_only=True)
    #   set on a ``MessageKind.MEMBERSHIP`` message only: THIS bot joined or left the room
    #   (0.1.6). ``conversation_id`` and ``sender`` are the room; ``author`` is empty.
    poll_vote: Optional["PollVote"] = field(default=None, kw_only=True)
    #   set on a ``MessageKind.POLL_VOTE`` message only (0.1.14): a vote on a poll THIS bot sent —
    #   who chose what, on which poll. ``selected=None`` means the vote was UNREAD (an empty or
    #   undecryptable WhatsApp vote), never an abstention. Reserved like the fields above: hidden
    #   from ``repr`` at its default (see ``_GROUP_FIELDS``), so a message without a vote prints
    #   exactly as before.

    def __repr__(self) -> str:
        """The generated dataclass ``repr``, minus the group fields while they hold their default.

        A 1:1 message therefore prints byte for byte as it did before the fields existed — the
        string a log line or a failing assertion shows does not grow six constant entries on
        every message — and a group message, the day an adapter fills them, shows exactly the
        ones that say something."""
        shown = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _GROUP_FIELDS and value == f.default:
                continue
            shown.append(f"{f.name}={value!r}")
        return f"{type(self).__qualname__}({', '.join(shown)})"


# The reserved fields above, by name — the ones ``InboundMessage.__repr__`` leaves out at their
# default. One definition, read by the repr and by the tests that pin the inertness.
# ``reply_to_message_id`` joined in 0.1.9 (the quoted id a host reads to decide ``reply_to_bot``)
# and ``mentioned_ids`` in 0.1.10 (the LIDs a host reads to decide ``mentions_bot``); ``poll_vote``
# in 0.1.14 (a vote on a poll this bot sent, set on a ``POLL_VOTE`` message only — not a group
# field, but reserved under the same rule). Each is additive and default on every ordinary message
# (``()`` for ``mentioned_ids``, ``None`` for ``poll_vote``), so hiding it at its default keeps
# that message's repr byte for byte as before the field existed.
_GROUP_FIELDS = ("conversation_id", "author", "mentions_bot", "mentioned_ids", "reply_to_bot",
                 "reply_to_message_id", "author_is_bot", "membership", "poll_vote")


@dataclass(frozen=True)
class Mention:
    """One member to @-mention in a GROUP reply (outbound).

    ``user_id`` is the member's id AT THE PROVIDER — a WhatsApp phone JID (``<digits>@s.whatsapp.net``)
    or LID (``<digits>@lid``), a Telegram numeric id — the same value a channel's ``author`` /
    ``mentioned_ids`` carry inbound. ``label`` is the member's display name, shown by a channel that
    cannot NOTIFY a mention, and also what a WhatsApp member sees in place of the ``@`` token (WhatsApp
    renders a mention as the contact's own name).

    Frozen, so a composed reply's mentions cannot be mutated after the host hands them over. How a
    mention APPEARS in the text is :meth:`Channel.mention_token`, never written by hand; at most
    :data:`MAX_MENTIONS` ride on one message (more raises ``ValueError`` at send)."""

    user_id: str
    label: str = ""


#: The most members one message may @-mention. The host never composes more; a send given more
#: raises ``ValueError`` — the net, not a path the host is expected to reach.
MAX_MENTIONS = 3


def label_mention_token(user_id: str, label: str) -> str:
    """The mention token for a channel that cannot NOTIFY a mentioned member — every channel but
    Evolution today (Telegram until it grows group mentions in a later piece, the WhatsApp Cloud API
    which has no groups, the web widget): the plain ``label``, no ``@``, so the reply reads naturally
    and nobody is pinged. The single definition of that rule, shared by those channels'
    ``mention_token``. ``user_id`` is unused but kept so every ``mention_token`` has the one signature
    the host calls."""
    return label


@dataclass
class OutboundMessage:
    """A reply to send back. ``audio`` carries voice-note bytes (e.g. from a
    cogno-vox TTS); ``media`` carries files to attach: a JPEG or PNG
    (``IMAGE_MIMES``) goes as a visible image, anything else as a document."""

    text: str = ""
    audio: Optional[bytes] = None
    audio_format: str = "opus"
    media: list[MediaRef] = field(default_factory=list)
    reaction: Optional[Reaction] = None
    buttons: list["Button"] = field(default_factory=list)   # quick-replies (≤3) under the text
    list_menu: Optional["ListMenu"] = None  # a menu of >3 options (WhatsApp list / TG keyboard)
    poll: Optional["Poll"] = None           # a WhatsApp poll (EvolutionChannel; others refuse)
    template: Optional["Template"] = None   # proactive send outside the 24h window
    # The provider id of the inbound message this reply answers. Telegram threads the FIRST text
    # chunk to it (``reply_parameters``), so a room sees whom the bot is answering; a value that
    # is not a positive integer is ignored. "" (the default) sends exactly as before. Other
    # adapters ignore it.
    reply_to_message_id: str = ""
    # Members to @-mention in a GROUP reply. Composed by the HOST, never the model: the host puts
    # each one's ``Channel.mention_token`` in ``text`` and lists the Mention here. Only
    # ``EvolutionChannel`` notifies (WhatsApp) — it sends ``mentioned`` on the chunk that carries the
    # tokens; the other channels send the text as-is and log ``mentions_unsupported``. At most
    # ``MAX_MENTIONS`` — more raises ``ValueError`` at send. ``[]`` (the default) sends as before.
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class ChannelConfig:
    """Per-tenant channel credentials/settings — host-injected (never from a DB
    inside the lib). Adapters read the fields they need."""

    token: str = ""        # telegram bot token / evolution api key
    base_url: str = ""     # evolution instance url (provider API base)
    instance: str = ""     # evolution instance name
    secret: str = ""       # webhook verification secret
    # When True, verify() FAILS CLOSED if no secret is configured (an inbound webhook is rejected
    # rather than trusted) — the SECURE DEFAULT for a provider webhook (Telegram / Evolution /
    # WhatsApp Cloud): a signature-capable channel must not silently run unverified, since anyone
    # who learns the URL could otherwise forge an inbound delivery.
    #
    # Two decisions, in order. It FIRST shipped False (2026-08): open-by-default with a warning,
    # from when this library's only client was our own host, which always passed the flag
    # explicitly. It is now True: a provider channel built without a secret rejects inbound by
    # default, and a deployment that genuinely has none opts OUT explicitly (``require_secret=
    # False``, dev/demo). The ``WebChannel`` is NOT a provider webhook — it is the host's own
    # widget route, guarded by the host's auth/CORS, with no third party to share a secret with —
    # so it stays OPEN by design and does not read this default (see the ``web`` branch of
    # ``create_channel``); failing it closed would break the web chat for no gain.
    # Three states (so the two kinds can have DIFFERENT secure defaults and an EXPLICIT choice is
    # honoured by every kind): ``None`` = unset, the kind's secure default — CLOSED for a provider
    # webhook, OPEN for the host's own web widget; ``True``/``False`` = the caller's explicit choice,
    # read the same by every kind. Read it through :meth:`secret_required`, never by truthiness.
    require_secret: Optional[bool] = None

    def secret_required(self, *, default: bool) -> bool:
        """The verification posture for ONE kind: an explicit ``require_secret`` wins for every
        kind; unset (``None``) is that kind's secure ``default`` (True for a provider webhook,
        False for the web widget)."""
        return default if self.require_secret is None else bool(self.require_secret)

    max_chars: int = 0     # outbound chunk size (0 → adapter default)
    # The READ budget: how long a provider may take to ANSWER. The connect budget is
    # separate and much shorter (``cogno_gateway.net``) — an address that never answers
    # is not the same risk as a provider taking its time, and one number for both is what
    # let a dead address spend a whole request.
    timeout: float = 15.0
    extra: dict = field(default_factory=dict)
    # ── Group chats (Telegram only, since 0.1.4). OFF by default, and off is exactly 0.1.3. ──
    # With ``accept_groups`` False every group shape drops at parse, unconditionally. True lets a
    # group ``message`` through with the group fields of ``InboundMessage`` filled; whether a
    # room may reach a turn at all (registered, enabled, which trigger) is the HOST's decision.
    accept_groups: bool = False
    # The bot's ``@handle`` (``getMe``), with or without the ``@``; any case. It is what an
    # ``@mention`` is compared with. Empty → no message ever reads as mentioning the bot.
    bot_username: str = ""


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str = ""
    # The provider id of a poll this send CREATED (Telegram ``sendPoll`` → ``result.poll.id``), by
    # which the provider later names votes on it: Telegram's ``poll_answer`` identifies a poll by
    # this id, not by the creating message. A host stores it to tie an inbound vote back to the
    # poll it answers. "" on every other send — a plain reply, and a WhatsApp poll too, since
    # WhatsApp carries no such id (a vote there is keyed by the creating message id). Additive:
    # a caller that never reads it sees exactly the previous result.
    poll_id: str = ""
