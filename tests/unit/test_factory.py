"""Unit tests for create_channel + Channel protocol conformance."""

import pytest

from cogno_gateway import (
    Channel,
    ChannelConfig,
    EvolutionChannel,
    GatewayError,
    TelegramChannel,
    WebChannel,
    create_channel,
)


def test_create_web_needs_no_config():
    assert isinstance(create_channel("web"), WebChannel)


def test_create_telegram():
    ch = create_channel("telegram", ChannelConfig(token="T"))
    assert isinstance(ch, TelegramChannel)


def test_create_whatsapp_aliases():
    cfg = ChannelConfig(base_url="http://e", token="k", instance="i")
    assert isinstance(create_channel("whatsapp", cfg), EvolutionChannel)
    assert isinstance(create_channel("evolution", cfg), EvolutionChannel)


def test_unknown_kind_raises():
    with pytest.raises(GatewayError):
        create_channel("carrier-pigeon", ChannelConfig())


def test_missing_config_raises():
    with pytest.raises(GatewayError):
        create_channel("telegram", None)


def test_all_channels_satisfy_protocol():
    cfg = ChannelConfig(base_url="http://e", token="k", instance="i")
    for ch in (WebChannel(), TelegramChannel(ChannelConfig(token="t")),
               EvolutionChannel(cfg)):
        assert isinstance(ch, Channel)
        assert ch.name in ("web", "telegram", "whatsapp")
