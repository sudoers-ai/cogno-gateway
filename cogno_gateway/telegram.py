"""
cogno_gateway.telegram — the Telegram Bot API channel.

Ported clean-room from the parent ``cogno.gateways.telegram`` and made async
(httpx.AsyncClient), with the FastAPI/DB/feedback coupling removed: this adapter
only verifies, parses, fetches media, and sends. The host owns the webhook route
and injects the per-tenant bot token/secret via ``ChannelConfig``.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("cogno_gateway.telegram")

_API = "https://api.telegram.org"
_SECRET_HEADER = "x-telegram-bot-api-secret-token"


class TelegramChannel:
    name = "telegram"

    def __init__(self, config: ChannelConfig) -> None:
        if not config.token:
            raise GatewayError("TelegramChannel requires config.token (bot token)")
        self._cfg = config
        self._token = config.token

    # ── verify ────────────────────────────────────────────────────────
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._cfg.secret:
            return True  # secret token not configured → host guards the route
        got = headers.get(_SECRET_HEADER) or headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        ok = got == self._cfg.secret
        if not ok:
            logger.warning("channel=telegram event=verify_failed reason=invalid_secret_token")
        return ok

    # ── parse inbound ─────────────────────────────────────────────────
    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        reaction = payload.get("message_reaction")
        if reaction:
            return self._parse_reaction(reaction)
        callback = payload.get("callback_query")
        if callback:
            return self._parse_callback(callback)
        message = payload.get("message")
        if not message:
            return None  # edits/etc. — ignored
        return self._parse_message(message)

    def _parse_callback(self, callback: dict) -> Optional[InboundMessage]:
        chat = (callback.get("message", {}) or {}).get("chat", {})
        data = callback.get("data", "")
        # Telegram echoes only callback_data; the title is not resent.
        return InboundMessage(
            channel=self.name, sender=str(chat.get("id", "")), kind=MessageKind.INTERACTIVE,
            message_id=str((callback.get("message", {}) or {}).get("message_id", "")),
            text=data, selection=ButtonReply(id=data, title=data), raw=callback)

    def _parse_reaction(self, reaction: dict) -> Optional[InboundMessage]:
        chat = reaction.get("chat", {})
        if chat.get("type") in ("group", "supergroup"):
            return None  # reactions are 1:1 feedback only
        new = reaction.get("new_reaction", [])
        if not new or new[0].get("type") != "emoji":
            return None
        return InboundMessage(
            channel=self.name, sender=str(chat.get("id", "")), kind=MessageKind.REACTION,
            reaction=Reaction(emoji=new[0].get("emoji", ""),
                              target_message_id=str(reaction.get("message_id", ""))),
            raw=reaction,
        )

    def _parse_message(self, message: dict) -> Optional[InboundMessage]:
        chat = message.get("chat", {})
        sender = str(chat.get("id", ""))
        message_id = str(message.get("message_id", ""))
        reply_to = (message.get("reply_to_message", {}) or {}).get("text", "") or ""

        def mk(kind: MessageKind, *, text: str = "",
               media: Optional[MediaRef] = None) -> InboundMessage:
            return InboundMessage(channel=self.name, sender=sender, kind=kind,
                                  message_id=message_id, text=text, media=media,
                                  reply_to=reply_to, raw=message)

        if message.get("text"):
            return mk(MessageKind.TEXT, text=message["text"])
        if "voice" in message or "audio" in message:
            a = message.get("voice") or message.get("audio") or {}
            return mk(MessageKind.AUDIO, text=message.get("caption", ""),
                      media=MediaRef(ref=a.get("file_id", ""),
                                     mime=a.get("mime_type", "audio/ogg")))
        if "photo" in message:
            photo = message["photo"][-1] if message["photo"] else {}  # largest size
            return mk(MessageKind.IMAGE, text=message.get("caption", ""),
                      media=MediaRef(ref=photo.get("file_id", ""), mime="image/jpeg"))
        if "document" in message:
            d = message["document"]
            return mk(MessageKind.DOCUMENT, text=message.get("caption", ""),
                      media=MediaRef(ref=d.get("file_id", ""), mime=d.get("mime_type", ""),
                                     filename=d.get("file_name", "")))
        return mk(MessageKind.UNKNOWN)

    # ── fetch media (getFile → download) ──────────────────────────────
    async def fetch_media(self, ref: MediaRef) -> bytes:
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            r = await client.get(f"{_API}/bot{self._token}/getFile",
                                  params={"file_id": ref.ref})
            r.raise_for_status()
            file_path = r.json().get("result", {}).get("file_path", "")
            if not file_path:
                raise GatewayError(f"Telegram getFile returned no file_path for {ref.ref!r}")
            dl = await client.get(f"{_API}/file/bot{self._token}/{file_path}")
            dl.raise_for_status()
            return dl.content

    # ── send ──────────────────────────────────────────────────────────
    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        ids: list[str] = []
        max_chars = self._cfg.max_chars or 600
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            try:
                if message.reaction:
                    await client.post(
                        f"{_API}/bot{self._token}/setMessageReaction",
                        json={"chat_id": recipient,
                              "message_id": int(message.reaction.target_message_id or 0),
                              "reaction": [{"type": "emoji", "emoji": message.reaction.emoji}]})
                chunks = split_message(message.text, max_chars=max_chars)
                markup = None
                # Telegram has no native list UI — render buttons and list rows alike
                # as an inline keyboard (one option per row).
                kb_buttons = list(message.buttons)
                if message.list_menu is not None:
                    kb_buttons += [r for s in message.list_menu.sections for r in s.rows]
                if kb_buttons:
                    markup = {"inline_keyboard": [
                        [{"text": b.title, "callback_data": b.id}] for b in kb_buttons]}
                    if not chunks:
                        chunks = [message.text or " "]   # buttons need a message body
                for i, chunk in enumerate(chunks):
                    body: dict = {"chat_id": recipient, "text": chunk}
                    if markup and i == len(chunks) - 1:
                        body["reply_markup"] = markup
                    resp = await client.post(f"{_API}/bot{self._token}/sendMessage", json=body)
                    resp.raise_for_status()
                    ids.append(str(resp.json().get("result", {}).get("message_id", "")))
                if message.audio is not None:
                    resp = await client.post(
                        f"{_API}/bot{self._token}/sendVoice",
                        data={"chat_id": recipient},
                        files={"voice": (f"voice.{message.audio_format}", message.audio,
                                         "audio/ogg")})
                    resp.raise_for_status()
                    ids.append(str(resp.json().get("result", {}).get("message_id", "")))
                for m in message.media:
                    await client.post(f"{_API}/bot{self._token}/sendDocument",
                                      json={"chat_id": recipient, "document": m.url or m.ref})
            except httpx.HTTPError as exc:
                logger.warning("channel=telegram event=send_failed sent=%d error=%s", len(ids), exc)
                return SendResult(ok=False, message_ids=ids, error=str(exc))
        logger.debug("channel=telegram event=message_sent chunks=%d ok=true", len(ids))
        return SendResult(ok=True, message_ids=ids)
