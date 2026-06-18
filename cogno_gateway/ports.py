"""
cogno_gateway.ports — the channel transport contract.

A ``Channel`` is pure transport: it verifies an inbound webhook, parses the
provider payload into an ``InboundMessage``, fetches referenced media bytes, and
sends an ``OutboundMessage`` back. It does **not** orchestrate the pipeline,
read a database, or run an HTTP server — the host owns the endpoint and wires the
parsed message into cognition (and cogno-vox for audio).
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, runtime_checkable

from cogno_gateway.types import InboundMessage, MediaRef, OutboundMessage, SendResult


class GatewayError(Exception):
    """Base class for transport errors raised by channel adapters."""


@runtime_checkable
class Channel(Protocol):
    """A messaging channel adapter (Telegram / WhatsApp / Web / …)."""

    name: str

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """True if the inbound webhook is authentic (signature/secret check).
        Fail-closed: a host should reject when this returns False."""

    def parse_inbound(self, payload: dict) -> Optional[InboundMessage]:
        """Normalize a provider payload → ``InboundMessage`` (or ``None`` to
        ignore it, e.g. a bot's own echo / an unsupported event)."""

    async def fetch_media(self, ref: MediaRef) -> bytes:
        """Download the bytes for a media reference (file id / URL)."""

    async def send(self, recipient: str, message: OutboundMessage) -> SendResult:
        """Send a reply to ``recipient`` (chat id / remoteJid / session id)."""
