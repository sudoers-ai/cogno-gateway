"""
cogno_gateway.factory — build a channel adapter from a kind + config.

The host calls ``create_channel("telegram", cfg)`` with a per-tenant
``ChannelConfig``. Channels are pluggable behind the ``Channel`` port, so adding
the official ``WhatsAppCloudChannel`` later is a one-line registry entry.
"""

from __future__ import annotations

from cogno_gateway.cloud import WhatsAppCloudChannel
from cogno_gateway.evolution import EvolutionChannel
from cogno_gateway.ports import Channel, GatewayError
from cogno_gateway.telegram import TelegramChannel
from cogno_gateway.types import ChannelConfig
from cogno_gateway.web import WebChannel


def create_channel(kind: str, config: ChannelConfig | None = None) -> Channel:
    """Instantiate a channel adapter.

    ``kind`` ∈ {telegram, web, evolution / whatsapp (unofficial Evolution API),
    whatsapp_cloud / cloud / meta (official Meta WhatsApp Cloud API)}.
    """
    k = kind.lower()
    if k == "web":
        return WebChannel(secret=(config.secret if config else ""))
    if config is None:
        raise GatewayError(f"channel {kind!r} requires a ChannelConfig")
    if k == "telegram":
        return TelegramChannel(config)
    if k in ("whatsapp_cloud", "cloud", "meta"):
        return WhatsAppCloudChannel(config)
    if k in ("whatsapp", "evolution"):
        return EvolutionChannel(config)
    raise GatewayError(f"unknown channel kind: {kind!r}")
