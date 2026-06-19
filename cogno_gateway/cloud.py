"""
cogno_gateway.cloud — the official WhatsApp Cloud API channel (Meta Graph API).

The compliant, production WhatsApp adapter (vs the unofficial ``EvolutionChannel``).
Same ``Channel`` port; the host picks per tenant.

Reactive model fit: a user message opens a **24h customer-service window** in which
you reply **free-form** (text/media/reaction) for free. A **proactive** message
outside that window (e.g. an appointment reminder) must use a pre-approved
**template** — pass an ``OutboundMessage(template=Template(...))``. *Deciding*
free-form vs template (i.e. whether the window is open) is host policy; this
adapter supports both sends.

``ChannelConfig``: ``token`` = access token (Bearer), ``instance`` = phone number
id, ``secret`` = app secret (for the ``X-Hub-Signature-256`` HMAC),
``base_url`` = Graph base (default ``https://graph.facebook.com/v21.0``),
``extra["verify_token"]`` = the GET-subscription verify token.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping, Optional

import httpx

from cogno_gateway.chunker import split_message
from cogno_gateway.ports import GatewayError
from cogno_gateway.types import (
    ChannelConfig,
    InboundMessage,
    Location,
    MediaRef,
    MessageKind,
    OutboundMessage,
    Reaction,
    SendResult,
)

_DEFAULT_BASE = "https://graph.facebook.com/v21.0"
_TYPE_KINDS = {
    "image": MessageKind.IMAGE,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}


class WhatsAppCloudChannel:
    name = "whatsapp"

    def __init__(self, config: ChannelConfig) -> None:
        if not (config.token and config.instance):
            raise GatewayError(
                "WhatsAppCloudChannel requires config.token (access token) and "
                "config.instance (phone number id)")
        self._cfg = config
        self._base = (config.base_url or _DEFAULT_BASE).rstrip("/")
        self._phone_id = config.instance

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._cfg.token}", "Content-Type": "application/json"}

    # ── verify ────────────────────────────────────────────────────────
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """HMAC-SHA256 check of the request body (``X-Hub-Signature-256``)."""
        if not self._cfg.secret:
            return True
        sig = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256") or ""
        expected = "sha256=" + hmac.new(self._cfg.secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def verify_subscription(self, *, mode: str, token: str, challenge: str) -> Optional[str]:
        """For the GET webhook handshake: returns ``challenge`` iff the token
        matches ``extra['verify_token']`` (else ``None`` → host returns 403)."""
        want = str(self._cfg.extra.get("verify_token", ""))
        if mode == "subscribe" and want and token == want:
            return challenge
        return None

    # ── parse inbound (Graph API webhook) ─────────────────────────────
    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        try:
            value = payload["entry"][0]["changes"][0]["value"]
            message = value["messages"][0]
        except (KeyError, IndexError, TypeError):
            return None  # statuses / non-message events
        sender = str(message.get("from", ""))
        message_id = str(message.get("id", ""))
        mtype = message.get("type", "")

        def mk(kind: MessageKind, *, text: str = "", media: Optional[MediaRef] = None,
               reaction: Optional[Reaction] = None,
               location: Optional[Location] = None) -> InboundMessage:
            return InboundMessage(channel=self.name, sender=sender, kind=kind,
                                  message_id=message_id, text=text, media=media,
                                  reaction=reaction, location=location, raw=message)

        if mtype == "text":
            return mk(MessageKind.TEXT, text=message.get("text", {}).get("body", ""))
        if mtype == "reaction":
            r = message.get("reaction", {})
            return mk(MessageKind.REACTION,
                      reaction=Reaction(emoji=r.get("emoji", ""),
                                        target_message_id=str(r.get("message_id", ""))))
        if mtype == "location":
            loc = message.get("location", {})
            return mk(MessageKind.LOCATION,
                      location=Location(latitude=float(loc.get("latitude", 0.0)),
                                        longitude=float(loc.get("longitude", 0.0)),
                                        name=loc.get("name", "")))
        if mtype in _TYPE_KINDS:
            sub = message.get(mtype, {}) or {}
            return mk(_TYPE_KINDS[mtype], text=sub.get("caption", ""),
                      media=MediaRef(ref=str(sub.get("id", "")), mime=sub.get("mime_type", ""),
                                     filename=sub.get("filename", "")))
        return mk(MessageKind.UNKNOWN)

    # ── fetch media (media-id → url → bytes) ──────────────────────────
    async def fetch_media(self, ref: MediaRef) -> bytes:
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            meta = await client.get(f"{self._base}/{ref.ref}", headers=self._headers())
            meta.raise_for_status()
            url = meta.json().get("url", "")
            if not url:
                raise GatewayError(f"Cloud API returned no media url for {ref.ref!r}")
            dl = await client.get(url, headers={"Authorization": f"Bearer {self._cfg.token}"})
            dl.raise_for_status()
            return dl.content

    # ── send ──────────────────────────────────────────────────────────
    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        ids: list[str] = []
        url = f"{self._base}/{self._phone_id}/messages"
        max_chars = self._cfg.max_chars or 600
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            try:
                if message.template is not None:
                    ids.append(await self._post(client, url, self._template_body(recipient,
                                                                                 message.template)))
                if message.reaction:
                    await self._post(client, url, {
                        "messaging_product": "whatsapp", "to": recipient, "type": "reaction",
                        "reaction": {"message_id": message.reaction.target_message_id,
                                     "emoji": message.reaction.emoji}})
                for chunk in split_message(message.text, max_chars=max_chars):
                    ids.append(await self._post(client, url, {
                        "messaging_product": "whatsapp", "to": recipient, "type": "text",
                        "text": {"body": chunk}}))
                if message.audio is not None:
                    media_id = await self._upload_media(
                        client, message.audio, f"audio/{message.audio_format}",
                        f"voice.{message.audio_format}")
                    ids.append(await self._post(client, url, {
                        "messaging_product": "whatsapp", "to": recipient, "type": "audio",
                        "audio": {"id": media_id}}))
                for m in message.media:
                    ids.append(await self._post(client, url, {
                        "messaging_product": "whatsapp", "to": recipient, "type": "document",
                        "document": {"link": m.url or m.ref, "caption": m.caption}}))
            except httpx.HTTPError as exc:
                return SendResult(ok=False, message_ids=ids, error=str(exc))
        return SendResult(ok=True, message_ids=[i for i in ids if i])

    def _template_body(self, recipient: str, template) -> dict:
        components = []
        if template.params:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": p} for p in template.params]})
        return {"messaging_product": "whatsapp", "to": recipient, "type": "template",
                "template": {"name": template.name, "language": {"code": template.lang},
                             "components": components}}

    async def _post(self, client: httpx.AsyncClient, url: str, body: dict) -> str:
        resp = await client.post(url, headers=self._headers(), json=body)
        resp.raise_for_status()
        msgs = resp.json().get("messages", [])
        return str(msgs[0].get("id", "")) if msgs else ""

    async def _upload_media(self, client: httpx.AsyncClient, data: bytes, mime: str,
                            filename: str) -> str:
        resp = await client.post(
            f"{self._base}/{self._phone_id}/media",
            headers={"Authorization": f"Bearer {self._cfg.token}"},
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)})
        resp.raise_for_status()
        return str(resp.json().get("id", ""))
