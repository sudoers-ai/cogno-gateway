"""
cogno_gateway.web — the Web/HTTP channel.

The simplest adapter: no external provider, no httpx. It (de)serializes the
host's own front-end JSON contract — the one cogno-cloud-ui's chat widget already
speaks (``POST /api/v1/webchat/landing``): inbound ``{session_id, message}`` and
outbound ``{session_id, response}`` — into the normalized message model. The host
owns the HTTP endpoint; this just translates.

The contract is extended (optional) for rich content: an inbound ``kind`` +
``media`` (url/mime) + ``reaction`` (emoji/target), and an outbound that can carry
``media`` and an audio URL — so the same widget can grow images/files/reactions.
"""

from __future__ import annotations

from typing import Mapping, Optional

from cogno_gateway.types import (
    InboundMessage,
    MediaRef,
    MessageKind,
    OutboundMessage,
    Reaction,
    SendResult,
)


class WebChannel:
    """Bridges a host JSON chat endpoint to the normalized message model.

    ``send`` does not call out anywhere — it serializes the reply to the dict the
    host returns in the HTTP response. Stateless; safe to share across requests.
    """

    name = "web"

    def __init__(self, *, secret: str = "") -> None:
        self._secret = secret

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """Optional shared-secret check (e.g. an ``X-Webchat-Secret`` header). With
        no secret configured it is open (the host's auth/CORS guards the route)."""
        if not self._secret:
            return True
        token = headers.get("x-webchat-secret") or headers.get("X-Webchat-Secret") or ""
        return token == self._secret

    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        session_id = str(payload.get("session_id") or payload.get("sender") or "")
        if not session_id:
            return None
        message_id = str(payload.get("message_id", ""))
        raw_kind = str(payload.get("kind", "") or "").lower()

        # Reaction event
        react = payload.get("reaction")
        if react:
            return InboundMessage(
                channel=self.name, sender=session_id, kind=MessageKind.REACTION,
                message_id=message_id,
                reaction=Reaction(emoji=str(react.get("emoji", "")),
                                  target_message_id=str(react.get("target_message_id", ""))),
                raw=payload,
            )

        # Media event (image/audio/document/…)
        media = payload.get("media")
        if media:
            kind = _MEDIA_KINDS.get(raw_kind, MessageKind.DOCUMENT)
            return InboundMessage(
                channel=self.name, sender=session_id, kind=kind, message_id=message_id,
                text=str(payload.get("message", "") or ""),
                media=MediaRef(ref=str(media.get("ref", "") or media.get("url", "")),
                               mime=str(media.get("mime", "")), url=str(media.get("url", "")),
                               caption=str(media.get("caption", ""))),
                raw=payload,
            )

        # Plain text (the cogno-cloud-ui contract)
        text = str(payload.get("message", "") or "")
        if not text:
            return None
        return InboundMessage(
            channel=self.name, sender=session_id, kind=MessageKind.TEXT,
            message_id=message_id, text=text, raw=payload,
        )

    async def fetch_media(self, ref: MediaRef) -> bytes:
        raise NotImplementedError(
            "WebChannel media bytes are uploaded by the host's front-end; pass them "
            "directly rather than fetching by reference."
        )

    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        # No outbound call — the host returns this dict in the HTTP response.
        _ = self.serialize(recipient, message)
        return SendResult(ok=True)

    def serialize(self, recipient: str, message: OutboundMessage) -> dict:
        """The JSON the host returns to the widget (``{session_id, response, …}``)."""
        out: dict = {"session_id": recipient, "response": message.text}
        if message.media:
            out["media"] = [{"url": m.url or m.ref, "mime": m.mime, "caption": m.caption}
                            for m in message.media]
        if message.audio is not None:
            out["audio_format"] = message.audio_format
        if message.reaction:
            out["reaction"] = {"emoji": message.reaction.emoji,
                               "target_message_id": message.reaction.target_message_id}
        if message.buttons:
            out["buttons"] = [{"id": b.id, "title": b.title} for b in message.buttons]
        if message.list_menu is not None:
            out["list"] = {"button": message.list_menu.button,
                           "sections": [{"title": s.title,
                                         "rows": [{"id": r.id, "title": r.title} for r in s.rows]}
                                        for s in message.list_menu.sections]}
        return out


_MEDIA_KINDS = {
    "image": MessageKind.IMAGE,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}
