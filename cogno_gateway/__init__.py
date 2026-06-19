"""
cogno-gateway — the messaging transport edge of the Cogno stack.

The sibling of cogno-vox (audio edge): where vox converts audio ⇆ text, gateway
converts **channel messages ⇆ a normalized model**. Pure transport behind a
``Channel`` port + httpx adapters (Telegram, WhatsApp/Evolution, Web) — it never
orchestrates the pipeline, reads a DB, or runs an HTTP server. The host owns the
endpoint and wires parsed messages into cognition (and cogno-vox for audio).

    webhook → verify + parse_inbound → [audio? fetch_media → vox STT] → pipeline
            → reply → [vox TTS] → send
"""

from cogno_gateway.types import (
    Button,
    ButtonReply,
    ChannelConfig,
    InboundMessage,
    ListMenu,
    ListSection,
    Location,
    MediaRef,
    MessageKind,
    OutboundMessage,
    Reaction,
    SendResult,
    Template,
)
from cogno_gateway.ports import Channel, GatewayError
from cogno_gateway.chunker import split_message
from cogno_gateway.web import WebChannel
from cogno_gateway.telegram import TelegramChannel
from cogno_gateway.evolution import EvolutionChannel
from cogno_gateway.cloud import WhatsAppCloudChannel
from cogno_gateway.factory import create_channel

__all__ = [
    "MessageKind",
    "InboundMessage",
    "OutboundMessage",
    "MediaRef",
    "Reaction",
    "Location",
    "Button",
    "ButtonReply",
    "ListSection",
    "ListMenu",
    "Template",
    "ChannelConfig",
    "SendResult",
    "Channel",
    "GatewayError",
    "split_message",
    "WebChannel",
    "TelegramChannel",
    "EvolutionChannel",
    "WhatsAppCloudChannel",
    "create_channel",
]
