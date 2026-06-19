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
from typing import Mapping, Optional

import httpx

from cogno_gateway.chunker import split_message
from cogno_gateway.ports import GatewayError
from cogno_gateway.types import (
    ButtonReply,
    ChannelConfig,
    InboundMessage,
    MediaRef,
    MessageKind,
    OutboundMessage,
    Reaction,
    SendResult,
)

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

    def _headers(self) -> dict:
        return {"apikey": self._cfg.token, "Content-Type": "application/json"}

    # ── verify ────────────────────────────────────────────────────────
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._cfg.secret:
            return True
        got = headers.get("apikey") or headers.get("authorization") or ""
        return got == self._cfg.secret

    # ── parse inbound (Evolution 'messages.upsert') ───────────────────
    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        if payload.get("event") != "messages.upsert":
            return None
        data = payload.get("data", {}) or {}
        key = data.get("key", {}) or {}
        if key.get("fromMe", False):
            return None  # our own echo
        sender = str(key.get("remoteJid", ""))
        if sender.endswith("@g.us"):
            return None  # ignore groups (1:1 only, mirrors the parent)
        message_id = str(key.get("id", ""))
        msg = data.get("message", {}) or {}
        msg_type = data.get("messageType", "")

        def mk(kind: MessageKind, *, text: str = "", media: Optional[MediaRef] = None,
               reaction: Optional[Reaction] = None,
               selection: Optional[ButtonReply] = None) -> InboundMessage:
            return InboundMessage(channel=self.name, sender=sender, kind=kind,
                                  message_id=message_id, text=text, media=media,
                                  reaction=reaction, selection=selection, raw=data)

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
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            r = await client.post(url, headers=self._headers(),
                                  json={"message": {"key": {"id": ref.ref}}})
            r.raise_for_status()
            b64 = r.json().get("base64", "")
            if not b64:
                raise GatewayError(f"Evolution returned no base64 for message {ref.ref!r}")
            return base64.b64decode(b64)

    # ── send ──────────────────────────────────────────────────────────
    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        ids: list[str] = []
        number = recipient.split("@", 1)[0]
        max_chars = self._cfg.max_chars or 600
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            try:
                if message.reaction:
                    await client.post(
                        f"{self._base}/message/sendReaction/{self._instance}",
                        headers=self._headers(),
                        json={"key": {"remoteJid": recipient, "fromMe": False,
                                      "id": message.reaction.target_message_id},
                              "reaction": message.reaction.emoji})
                if message.list_menu is not None:
                    resp = await client.post(
                        f"{self._base}/message/sendList/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "title": "", "description": message.text or " ",
                              "buttonText": message.list_menu.button,
                              "sections": [
                                  {"title": s.title,
                                   "rows": [{"rowId": r.id, "title": r.title, "description": ""}
                                            for r in s.rows]}
                                  for s in message.list_menu.sections]})
                    resp.raise_for_status()
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                elif message.buttons:
                    resp = await client.post(
                        f"{self._base}/message/sendButtons/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "title": "", "description": message.text or " ",
                              "buttons": [{"type": "reply", "displayText": b.title, "id": b.id}
                                          for b in message.buttons]})
                    resp.raise_for_status()
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                else:
                    for chunk in split_message(message.text, max_chars=max_chars):
                        resp = await client.post(
                            f"{self._base}/message/sendText/{self._instance}",
                            headers=self._headers(), json={"number": number, "text": chunk})
                        resp.raise_for_status()
                        ids.append(str(resp.json().get("key", {}).get("id", "")))
                if message.audio is not None:
                    audio_b64 = base64.b64encode(message.audio).decode("ascii")
                    resp = await client.post(
                        f"{self._base}/message/sendWhatsAppAudio/{self._instance}",
                        headers=self._headers(), json={"number": number, "audio": audio_b64})
                    resp.raise_for_status()
                    ids.append(str(resp.json().get("key", {}).get("id", "")))
                for m in message.media:
                    await client.post(
                        f"{self._base}/message/sendMedia/{self._instance}",
                        headers=self._headers(),
                        json={"number": number, "media": m.url or m.ref,
                              "mediatype": "document", "caption": m.caption})
            except httpx.HTTPError as exc:
                return SendResult(ok=False, message_ids=ids, error=str(exc))
        return SendResult(ok=True, message_ids=ids)
