"""
cogno_gateway.evolution — the WhatsApp channel via the Evolution API.

Ported clean-room from the parent ``cogno.gateways.whatsapp`` (Evolution API),
async + decoupled from FastAPI/DB/feedback. Evolution is an **unofficial**
WhatsApp gateway (Baileys/QR) — free, full-featured, ban-risk accepted; good for
dev/testing. The official, compliant alternative is ``WhatsAppCloudChannel``
(``cogno_gateway.cloud``), which satisfies the same ``Channel`` port — the host
picks the provider per tenant.

Host injects per-tenant Evolution creds via ``ChannelConfig``: ``base_url``
(instance URL), ``token`` (apikey), ``instance`` (instance name), ``secret``
(webhook secret).
"""

from __future__ import annotations

import base64
import hmac
import logging
from typing import Mapping, Optional

import httpx

from cogno_gateway.chunker import split_message
from cogno_gateway.markup import log_outbound_markup, to_channel_markup
from cogno_gateway.net import build_async_client
from cogno_gateway.retry import post_with_retry
from cogno_gateway.ports import GatewayError
from cogno_gateway.types import (
    ButtonReply,
    ChannelConfig,
    InboundMessage,
    MediaRef,
    MessageKind,
    OutboundMessage,
    PresenceEvent,
    Reaction,
    SendResult,
)

logger = logging.getLogger("cogno_gateway.evolution")

_MEDIA_TYPES = {
    "imageMessage": MessageKind.IMAGE,
    "audioMessage": MessageKind.AUDIO,
    "videoMessage": MessageKind.VIDEO,
    "documentMessage": MessageKind.DOCUMENT,
    "stickerMessage": MessageKind.STICKER,
}


class EvolutionChannel:
    name = "whatsapp"

    def __init__(self, config: ChannelConfig) -> None:
        if not (config.base_url and config.token and config.instance):
            raise GatewayError(
                "EvolutionChannel requires config.base_url, config.token (apikey) and config.instance")
        self._cfg = config
        self._base = config.base_url.rstrip("/")
        self._instance = config.instance
        if not config.secret:
            if config.secret_required(default=True):
                logger.warning(
                    "channel=whatsapp event=verify_fail_closed reason=no_secret_configured — "
                    "inbound is REJECTED by default without a webhook secret; configure one, "
                    "or pass require_secret=False to accept unverified inbound (dev/demo only)")
            else:
                logger.warning("channel=whatsapp event=verify_open reason=no_secret_configured")

    def _headers(self) -> dict:
        return {"apikey": self._cfg.token, "Content-Type": "application/json"}

    # ── verify ────────────────────────────────────────────────────────
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._cfg.secret:
            if self._cfg.secret_required(default=True):   # production: no secret → reject forged deliveries
                logger.warning("channel=whatsapp event=verify_denied reason=secret_required — "
                               "configure a webhook secret, or pass require_secret=False to "
                               "accept unverified inbound (dev/demo only)")
                return False
            return True
        got = headers.get("apikey") or headers.get("authorization") or ""
        ok = hmac.compare_digest(got.encode(), self._cfg.secret.encode())
        if not ok:
            logger.warning("channel=whatsapp event=verify_failed reason=invalid_apikey")
        return ok

    # ── parse presence (Evolution 'presence.update') ──────────────────
    def parse_presence(self, payload: dict) -> Optional[PresenceEvent]:
        """``presence.update`` → :class:`PresenceEvent`, else ``None``.

        A SIBLING of :meth:`parse_inbound`, not a branch of it: presence is not a message, so
        letting it through the message parser would give it a turn, a dedup claim and a place
        in history that it must never have. ``parse_inbound`` keeps returning ``None`` here,
        and its test pinning that stays true.

        The payload nests the state under ``data.presences[jid].lastKnownPresence`` (Baileys
        shape). The jid is read from the map's own key rather than from ``data.id`` because a
        group payload carries several, and we take only the one matching ``data.id`` — a group
        presence must not move a 1:1 deadline. Groups are ignored entirely, exactly as
        :meth:`parse_inbound` ignores ``@g.us``.

        Requires the host to subscribe ``PRESENCE_UPDATE`` on the webhook; without it this
        method is simply never called, which is a supported state, not a failure.
        """
        if payload.get("event") != "presence.update":
            return None
        data = payload.get("data", {}) or {}
        sender = str(data.get("id", "") or "")
        if not sender or sender.endswith("@g.us"):
            return None
        presences = data.get("presences", {}) or {}
        entry = presences.get(sender)
        if not isinstance(entry, dict):
            # Some builds key the map by a normalised jid; fall back to the sole entry when
            # there is exactly one, and give up rather than guess when there are several.
            values = [v for v in presences.values() if isinstance(v, dict)]
            if len(values) != 1:
                return None
            entry = values[0]
        state = str(entry.get("lastKnownPresence", "") or "").strip().lower()
        if not state:
            return None
        return PresenceEvent(channel=self.name, sender=sender, state=state)

    # ── parse inbound (Evolution 'messages.upsert') ───────────────────
    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        if payload.get("event") != "messages.upsert":
            return None
        data = payload.get("data", {}) or {}
        key = data.get("key", {}) or {}
        if key.get("fromMe", False):
            return None  # our own echo
        sender = str(key.get("remoteJid", ""))
        is_group = sender.endswith("@g.us")
        if is_group and not self._cfg.accept_groups:
            return None  # groups OFF (the default): drop at parse, exactly as before 0.1.x
        group_conversation_id = ""
        group_author = ""
        reply_to_message_id = ""
        group_mentioned_ids: tuple[str, ...] = ()
        if is_group:
            # The AUTHOR. `key.participant` is ALWAYS `<lid>@lid` (addressingMode="lid"); the
            # routable phone is the bridge `key.participantAlt = <phone>@s.whatsapp.net`. Invariant
            # (a): the author comes ONLY from a phone JID — no `participantAlt`, an `@lid`, or any
            # other host → SILENCE. The `@lid` is never promoted (there is no `@lid`→phone bridge to
            # resolve it with); a registered member's phone JID resolves on the host with no change
            # there (`channels._wa_same` already collapses it to the stored contact).
            participant_alt = str(key.get("participantAlt", "") or "")
            if not participant_alt.endswith("@s.whatsapp.net"):
                return None
            group_author = participant_alt
            # Invariant (b): a group message ALWAYS leaves with `conversation_id` set (== sender ==
            # the room), so a host can never read it as a 1:1 and answer the room as a person.
            group_conversation_id = sender
            # The reply (additive, PURE): expose the QUOTED id. `contextInfo` is a TOP-LEVEL
            # sibling of `message` (`data.contextInfo`) — null on a plain message, carrying
            # `stanzaId` on a reply — read independently of `messageType`. The parser only exposes
            # the id; the HOST decides `reply_to_bot` against the room's buffer (`bot:<id>` lines).
            # `mentions_bot` is deliberately NOT set: a WhatsApp mention is by the bot's own LID,
            # which the PURE parser does not know. The parser only EXPOSES the mentioned LIDs
            # (`contextInfo.mentionedJid`, additive) in `mentioned_ids`, verbatim; the HOST decides
            # `mentions_bot` by matching them against the room's stored `bot_lid`.
            ctx = data.get("contextInfo")
            if isinstance(ctx, dict):
                reply_to_message_id = str(ctx.get("stanzaId", "") or "")
                mentioned = ctx.get("mentionedJid")
                if isinstance(mentioned, list):
                    group_mentioned_ids = tuple(str(j) for j in mentioned if j)
        message_id = str(key.get("id", ""))
        msg = data.get("message", {}) or {}
        msg_type = data.get("messageType", "")

        def mk(kind: MessageKind, *, text: str = "", media: Optional[MediaRef] = None,
               reaction: Optional[Reaction] = None,
               selection: Optional[ButtonReply] = None) -> InboundMessage:
            return InboundMessage(channel=self.name, sender=sender, kind=kind,
                                  message_id=message_id, text=text, media=media,
                                  reaction=reaction, selection=selection, raw=data,
                                  conversation_id=group_conversation_id, author=group_author,
                                  reply_to_message_id=reply_to_message_id,
                                  mentioned_ids=group_mentioned_ids)

        if "reactionMessage" in msg:
            rm = msg["reactionMessage"]
            return mk(MessageKind.REACTION,
                      reaction=Reaction(emoji=rm.get("text", ""),
                                        target_message_id=str(rm.get("key", {}).get("id", ""))))
        if "buttonsResponseMessage" in msg:
            br = msg["buttonsResponseMessage"]
            return mk(MessageKind.INTERACTIVE, text=br.get("selectedDisplayText", ""),
                      selection=ButtonReply(id=str(br.get("selectedButtonId", "")),
                                            title=br.get("selectedDisplayText", "")))
        if "listResponseMessage" in msg:
            lr = msg["listResponseMessage"]
            row = lr.get("singleSelectReply", {}) or {}
            return mk(MessageKind.INTERACTIVE, text=lr.get("title", ""),
                      selection=ButtonReply(id=str(row.get("selectedRowId", "")),
                                            title=lr.get("title", "")))
        if msg_type == "conversation":
            return mk(MessageKind.TEXT, text=msg.get("conversation", ""))
        if msg_type == "extendedTextMessage":
            return mk(MessageKind.TEXT, text=msg.get("extendedTextMessage", {}).get("text", ""))
        if msg_type in _MEDIA_TYPES:
            sub = msg.get(msg_type, {}) or {}
            return mk(_MEDIA_TYPES[msg_type], text=sub.get("caption", ""),
                      media=MediaRef(ref=message_id, mime=sub.get("mimetype", ""),
                                     url=sub.get("url", "")))
        return mk(MessageKind.UNKNOWN)

    # ── fetch media (getBase64FromMediaMessage) ───────────────────────
    async def fetch_media(self, ref: MediaRef) -> bytes:
        url = f"{self._base}/chat/getBase64FromMediaMessage/{self._instance}"
        async with build_async_client(self._cfg) as client:
            r = await client.post(url, headers=self._headers(),
                                  json={"message": {"key": {"id": ref.ref}}})
            r.raise_for_status()
            b64 = r.json().get("base64", "")
            if not b64:
                raise GatewayError(f"Evolution returned no base64 for message {ref.ref!r}")
            return base64.b64decode(b64)

    # ── group participants + own owner candidates (W4 mention; host picks the bot's LID) ──
    async def group_participants(
        self, group_jid: str, *, timeout: float = 3.0
    ) -> "tuple[tuple[str, ...], tuple[tuple[str, str], ...]]":
        """This instance's ``ownerJid`` CANDIDATES and a room's members, for the host to pick the
        bot's own LID and decide a WhatsApp @mention. Two reads, no retry, a short timeout: a host
        asks it beside a room's registration, NEVER on a turn.

        1. ``GET /instance/fetchInstances?instanceName=<instance>`` → a LIST of FLAT items
           (measured 2026-11-12). The ``ownerJid`` of EVERY item whose ``name == self._instance``
           is returned, as a tuple — our own filter, since the server's ``?instanceName=`` was not
           proved to narrow. The host applies «exactly one, or nothing» (0 or more than 1 is
           ambiguous and never guessed; never the first of several). The item also carries
           ``token`` (a secret) and ``number`` (a line): neither is ever read, returned or logged —
           only ``name`` and ``ownerJid`` are.
        2. ``GET /group/participants/{instance}?groupJid=<group>`` →
           ``{"participants": [{"id": "<lid>@lid", "phoneNumber": "<phone>@s.whatsapp.net", …}]}``;
           returned as ``(id, phoneNumber)`` pairs, verbatim.

        Any failure (non-2xx, unreadable, timeout) → ``((), ())``: the host then stores no
        ``bot_lid`` and a mention never fires — the room is deaf and the host says so, never a
        guessed LID. The owner/member matching and the ``bot_lid`` choice are the host's PURE
        function; this method only reads."""
        try:
            async with build_async_client(self._cfg, timeout=timeout) as client:
                fi = await client.get(
                    f"{self._base}/instance/fetchInstances",
                    headers=self._headers(), params={"instanceName": self._instance})
                gp = await client.get(
                    f"{self._base}/group/participants/{self._instance}",
                    headers=self._headers(), params={"groupJid": group_jid})
        except Exception as exc:  # noqa: BLE001 — a read that cannot complete is unknown, not a crash
            logger.warning("channel=whatsapp event=group_participants_failed error=%s",
                           type(exc).__name__)
            return (), ()
        return self._own_owner_jids(fi), self._members_of(gp)

    def _own_owner_jids(self, resp: "httpx.Response") -> "tuple[str, ...]":
        """The ``ownerJid`` of EVERY fetchInstances item whose ``name == self._instance`` (our own
        filter). The body is a LIST of flat items (measured); a nested ``{instance: {…}}`` item is
        tolerated too. ``token``/``number`` are never read. The «exactly one, or nothing» rule is
        the host's — it needs the count to tell 0 (no match) from >1 (ambiguous)."""
        if resp.status_code // 100 != 2:
            return ()
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — unreadable body → nothing
            return ()
        items = body if isinstance(body, list) else [body]
        owners: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            inner = it.get("instance") if isinstance(it.get("instance"), dict) else it
            name = str(inner.get("name", inner.get("instanceName", "")) or "")
            if name == self._instance:
                owners.append(str(inner.get("ownerJid", "") or ""))
        return tuple(owners)

    @staticmethod
    def _members_of(resp: "httpx.Response") -> "tuple[tuple[str, str], ...]":
        """``(id, phoneNumber)`` for each participant, verbatim; ``()`` on any non-2xx / unreadable
        / shapeless body."""
        if resp.status_code // 100 != 2:
            return ()
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return ()
        parts = body.get("participants") if isinstance(body, dict) else body
        if not isinstance(parts, list):
            return ()
        return tuple((str(p.get("id", "") or ""), str(p.get("phoneNumber", "") or ""))
                     for p in parts if isinstance(p, dict))

    async def _post(self, client: "httpx.AsyncClient", url: str, **kwargs):
        """One send POST, retried only on the write-safe failures (see cogno_gateway.retry):
        a connection failure, a 429 or a 502/503/504 — never a read timeout, which could
        duplicate a message Evolution already accepted."""
        return await post_with_retry("whatsapp", client, url, **kwargs)

    # ── send ──────────────────────────────────────────────────────────
    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        ids: list[str] = []
        # A hidden-phone sender arrives as `<digits>@lid` (not `@s.whatsapp.net`) — the bare
        # digits are NOT a phone number and don't route, so keep the full JID for those.
        number = recipient if recipient.endswith("@lid") else recipient.split("@", 1)[0]
        max_chars = self._cfg.max_chars or 600
        # WhatsApp's bold is a SINGLE asterisk; the voicer writes markdown's double one. Convert
        # once, here, before the text is chunked or used as a menu/button body — those two
        # branches never reach the chunker, so a conversion placed in the loop alone would leave
        # them behind.
        text = to_channel_markup(message.text, self.name)
        log_outbound_markup(logger, "whatsapp", message.text, text)
        async with build_async_client(self._cfg) as client:
            try:
                if message.reaction:
                    resp = await self._post(client,
                        f"{self._base}/message/sendReaction/{self._instance}",
                        headers=self._headers(),
                        json={"key": {"remoteJid": recipient, "fromMe": False,
                                      "id": message.reaction.target_message_id},
                              "reaction": message.reaction.emoji})
                if message.list_menu is not None:
                    resp = await self._post(client,
                        f"{self._base}/message/sendList/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "title": "", "description": text or " ",
                              "buttonText": message.list_menu.button,
                              "sections": [
                                  {"title": s.title,
                                   "rows": [{"rowId": r.id, "title": r.title, "description": ""}
                                            for r in s.rows]}
                                  for s in message.list_menu.sections]})
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                elif message.buttons:
                    resp = await self._post(client,
                        f"{self._base}/message/sendButtons/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "title": "", "description": text or " ",
                              "buttons": [{"type": "reply", "displayText": b.title, "id": b.id}
                                          for b in message.buttons]})
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                else:
                    for chunk in split_message(text, max_chars=max_chars):
                        resp = await self._post(client,
                            f"{self._base}/message/sendText/{self._instance}",
                            headers=self._headers(), json={"number": number, "text": chunk})
                        ids.append(str(resp.json().get("key", {}).get("id", "")))
                if message.audio is not None:
                    audio_b64 = base64.b64encode(message.audio).decode("ascii")
                    resp = await self._post(client,
                        f"{self._base}/message/sendWhatsAppAudio/{self._instance}",
                        headers=self._headers(), json={"number": number, "audio": audio_b64})
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                for m in message.media:
                    resp = await self._post(client,
                        f"{self._base}/message/sendMedia/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "media": m.url or m.ref,
                              "mediatype": "document", "caption": m.caption})
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
            except httpx.HTTPError as exc:
                logger.warning("channel=whatsapp event=send_failed sent=%d error=%s", len(ids), exc)
                return SendResult(ok=False, message_ids=ids, error=str(exc))
        logger.debug("channel=whatsapp event=message_sent chunks=%d ok=true", len(ids))
        return SendResult(ok=True, message_ids=ids)
