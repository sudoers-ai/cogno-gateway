"""
cogno_gateway.telegram — the Telegram Bot API channel.

Ported clean-room from the parent ``cogno.gateways.telegram`` and made async
(httpx.AsyncClient), with the FastAPI/DB/feedback coupling removed: this adapter
only verifies, parses, fetches media, and sends. The host owns the webhook route
and injects the per-tenant bot token/secret via ``ChannelConfig``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Mapping, Optional

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

# One retry, and a short pause before it. Short because the contact is waiting on the other
# side of this call and the host is holding a turn open; a long backoff turns a lost reply
# into a late one, which on a chat channel is barely better.
_RETRY_BACKOFF_SECONDS = 0.5


def _error_detail(exc: "httpx.HTTPError") -> str:
    """The Telegram-reported reason for a failed send, not the opaque httpx status. On a 4xx,
    ``raise_for_status`` raises ``HTTPStatusError`` whose ``response`` body carries Telegram's
    ``{"ok": false, "description": "..."}`` (e.g. "chat not found", "can't parse entities …") —
    surface that so a failure is diagnosable.

    Transport errors have no response and no ``description``, and — measured — usually no message
    either: httpx maps httpcore's timeout to ``ReadTimeout(str(inner))`` and that inner exception
    is a bare ``TimeoutError``, so ``str(exc)`` is ``""``. The line then read ``error=`` and a read
    timeout was **indistinguishable from no error at all**. Fall back to the exception CLASS, which
    is the whole diagnosis for this family: ``ReadTimeout`` says the request went out and no answer
    came back, ``ConnectError`` says it never left. Never the empty string."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            desc = resp.json().get("description")
        except Exception:  # noqa: BLE001 — non-JSON body → use raw text
            desc = None
        detail = desc or (resp.text or "").strip()
        if detail:
            return f"{resp.status_code} {detail}"
    return str(exc) or type(exc).__name__


def _is_transport_error(exc: BaseException) -> bool:
    """Did we fail to get an ANSWER, as opposed to getting an answer that said no?

    ``httpx.TransportError`` is the whole family where no HTTP response ever existed — connect
    and read timeouts, dropped connections, pool exhaustion, protocol errors. Everything else a
    send can raise (``HTTPStatusError`` from ``raise_for_status``, at any status) means the server
    answered, and an answer repeated is the same answer.

    This is the ONE predicate the retry turns on. It deliberately does not look at the status
    code: a 4xx is not retried because it is not a transport error, and neither is a 5xx — the
    server took the request and told us what happened to it."""
    return isinstance(exc, httpx.TransportError)


async def _post_once(client: "httpx.AsyncClient", url: str, **kwargs: Any) -> "httpx.Response":
    resp = await client.post(url, **kwargs)
    resp.raise_for_status()
    return resp


class TelegramChannel:
    name = "telegram"

    def __init__(self, config: ChannelConfig) -> None:
        if not config.token:
            raise GatewayError("TelegramChannel requires config.token (bot token)")
        self._cfg = config
        self._token = config.token
        if not config.secret:
            logger.warning("channel=telegram event=verify_open reason=no_secret_configured")


    # ── one HTTP call, with one retry on a transport failure ──────────
    async def _post(self, client: "httpx.AsyncClient", url: str,
                    **kwargs: Any) -> "httpx.Response":
        """POST once; on a TRANSPORT failure, pause briefly and POST exactly once more.

        **We cannot tell "it never arrived" from "it arrived and the answer was lost."** The
        Telegram Bot API has no idempotency key, no client-supplied message id and no cheap
        read-back of what it just accepted, so after a ``ReadTimeout`` the request is genuinely
        in superposition: the request may have been fully received and the reply delivered, and
        only the HTTP response lost on the way home. Nothing here can collapse that, and this
        docstring exists so nobody later reads the retry as if it could.

        Only one sub-family is unambiguous — ``ConnectError``/``ConnectTimeout``/``PoolTimeout``
        happen before any request bytes are written, so nothing could have been delivered. It is
        deliberately NOT used to narrow the retry, because the loss we measured was a read-phase
        timeout: a retry that only covered the provably-safe half would not have saved the
        message it was written for.

        So the choice is made openly, and it is a choice between two harms:

        * a duplicate — the contact sees the same reply twice, understands it in a second, and
          nothing is lost;
        * a silent loss — the contact's message showed as delivered on their side, so they have
          no reason to repeat it, and our reply is simply gone. Nobody notices and nothing
          recovers it.

        On a conversational text channel the duplicate is strictly the lesser harm, so we retry.
        The scope where that stops being true is a channel with per-message billing or a
        provider template (WhatsApp Cloud) — which is one reason this lands here and not there.

        The retry is per **HTTP call**, not per ``send()``, and that bound is the point: with a
        1558-char reply split into chunks, retrying the whole send would re-deliver every chunk
        that already succeeded, for certain. Retrying the one call that failed confines the
        maybe-duplicate to a single chunk."""
        try:
            return await _post_once(client, url, **kwargs)
        except httpx.HTTPError as exc:
            if not _is_transport_error(exc):
                raise
            # WARNING, not DEBUG: a retry is a handled/recovered condition (LOGGING.md), and it
            # is the only record that this reply may now exist twice on the contact's phone.
            logger.warning("channel=telegram event=send_retry attempt=2 after=%s",
                           _error_detail(exc))
        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        return await _post_once(client, url, **kwargs)

    # ── verify ────────────────────────────────────────────────────────
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._cfg.secret:
            if self._cfg.require_secret:   # production: no secret → reject forged updates
                logger.warning("channel=telegram event=verify_denied reason=secret_required")
                return False
            return True  # dev/demo: secret token not configured → host guards the route
        got = headers.get(_SECRET_HEADER) or headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        ok = hmac.compare_digest(got.encode(), self._cfg.secret.encode())
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

    @staticmethod
    def _is_group(chat: dict) -> bool:
        """Cogno is 1:1 by design (WhatsApp drops ``@g.us`` at parse — this is the Telegram
        mirror). Without it the only guard is BotFather's privacy mode, an external toggle:
        a @mention delivered from a group would run a turn keyed by the GROUP chat id — one
        shared auto-GUEST session/memory for every member. Drop group traffic in code."""
        return chat.get("type") in ("group", "supergroup")

    def _parse_callback(self, callback: dict) -> Optional[InboundMessage]:
        chat = (callback.get("message", {}) or {}).get("chat", {})
        if self._is_group(chat):
            return None  # 1:1 only
        data = callback.get("data", "")
        # Telegram echoes only callback_data; the title is not resent.
        return InboundMessage(
            channel=self.name, sender=str(chat.get("id", "")), kind=MessageKind.INTERACTIVE,
            message_id=str((callback.get("message", {}) or {}).get("message_id", "")),
            text=data, selection=ButtonReply(id=data, title=data), raw=callback)

    def _parse_reaction(self, reaction: dict) -> Optional[InboundMessage]:
        chat = reaction.get("chat", {})
        if self._is_group(chat):
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
        if self._is_group(chat):
            return None  # 1:1 only — see _is_group
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
        """Send a reply. Each provider call goes through :meth:`_post`, which retries once on a
        transport failure — read that method before changing anything here, the duplicate-message
        trade-off is written there.

        ``message_ids`` stays best-effort: when a call times out and its retry succeeds, the id
        recorded is the retry's. If the first attempt did land after all, that copy's id was
        never returned to us and cannot be listed. A missing id is not evidence a message was
        not delivered — nothing in this class can produce that evidence."""
        ids: list[str] = []
        max_chars = self._cfg.max_chars or 600
        async with httpx.AsyncClient(timeout=self._cfg.timeout) as client:
            try:
                if message.reaction:
                    await self._post(
                        client, f"{_API}/bot{self._token}/setMessageReaction",
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
                    resp = await self._post(client, f"{_API}/bot{self._token}/sendMessage",
                                            json=body)
                    ids.append(str(resp.json().get("result", {}).get("message_id", "")))
                if message.audio is not None:
                    resp = await self._post(
                        client, f"{_API}/bot{self._token}/sendVoice",
                        data={"chat_id": recipient},
                        files={"voice": (f"voice.{message.audio_format}", message.audio,
                                         "audio/ogg")})
                    ids.append(str(resp.json().get("result", {}).get("message_id", "")))
                for m in message.media:
                    resp = await self._post(client, f"{_API}/bot{self._token}/sendDocument",
                                            json={"chat_id": recipient,
                                                  "document": m.url or m.ref})
                    ids.append(str(resp.json().get("result", {}).get("message_id", "")))
            except httpx.HTTPError as exc:
                detail = _error_detail(exc)
                logger.warning("channel=telegram event=send_failed sent=%d error=%s", len(ids),
                               detail)
                return SendResult(ok=False, message_ids=ids, error=detail)
        logger.debug("channel=telegram event=message_sent chunks=%d ok=true", len(ids))
        return SendResult(ok=True, message_ids=ids)
