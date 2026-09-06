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


# ── the factory must forward require_secret (security fix) ───────────────────
# WebChannel honoured ``require_secret`` from its first day; the FACTORY dropped it, so a
# host that asked for verification through create_channel() got an open door and no warning.
# The class-level tests in test_web.py all build WebChannel directly, which is why the gap
# survived: nothing exercised the factory path.

def test_factory_web_forwards_require_secret():
    """require_secret=True + no secret + no headers → verify() must fail CLOSED."""
    ch = create_channel("web", ChannelConfig(require_secret=True))
    assert ch.verify(headers={}, body=b"") is False


def test_factory_web_still_accepts_a_correct_secret():
    """The positive twin: failing closed must not degenerate into refusing everything."""
    ch = create_channel("web", ChannelConfig(secret="s3cr3t-x", require_secret=True))
    assert ch.verify(headers={"x-webchat-secret": "s3cr3t-x"}, body=b"") is True
    assert ch.verify(headers={"x-webchat-secret": "wrong-x"}, body=b"") is False


def test_factory_web_stays_open_by_default():
    """Unchanged behaviour when the host does NOT ask for verification."""
    assert create_channel("web", ChannelConfig()).verify(headers={}, body=b"") is True
    assert create_channel("web").verify(headers={}, body=b"") is True


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("telegram", dict(token="tg-token-x")),
        ("web", dict()),
        ("evolution", dict(base_url="http://h.invalid", token="ev-key-x", instance="inst-x")),
        ("whatsapp_cloud", dict(token="cl-token-x", instance="12345")),
    ],
)
def test_every_channel_fails_closed_when_a_secret_is_required(kind, extra):
    """The family property, not a patch on one channel: whatever a host builds through the
    factory, asking for a secret and configuring none must never verify an unsigned request.
    Each kind gets the minimum config its constructor demands, so a missing-credential error
    can never be mistaken for the security property under test."""
    ch = create_channel(kind, ChannelConfig(require_secret=True, **extra))
    assert ch.verify(headers={}, body=b"") is False
