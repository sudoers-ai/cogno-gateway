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


# ── provider webhooks fail closed by default; the web widget stays open by design ────
# The two sides must NOT be "uniformised" later: a PROVIDER webhook (Telegram/Evolution/Cloud)
# with no secret is rejected by default (``require_secret`` now defaults True), because anyone
# who learns the URL could forge a delivery. The WEB widget is the host's OWN auth/CORS-guarded
# route, with no third party to share a secret with, so it stays OPEN by design — failing it
# closed would break the web chat for no gain.

def test_providers_fail_closed_but_web_stays_open_by_default():
    for kind, cfg in (("telegram", ChannelConfig(token="t")),
                      ("whatsapp", ChannelConfig(base_url="http://e", token="k", instance="i")),
                      ("whatsapp_cloud", ChannelConfig(token="t", instance="p"))):
        ch = create_channel(kind, cfg)
        assert ch.verify(headers={}, body=b"") is False, f"{kind} must fail closed by default"
    # web: no secret, config default require_secret=True → still OPEN (special-cased)
    assert create_channel("web", ChannelConfig()).verify(headers={}, body=b"") is True
    assert create_channel("web", None).verify(headers={}, body=b"") is True


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
        # web is deliberately NOT here: it is the host's own widget route, open by design, and
        # does not fail closed on a missing secret (see the both-sides test above).
        ("telegram", dict(token="tg-token-x")),
        ("evolution", dict(base_url="http://h.invalid", token="ev-key-x", instance="inst-x")),
        ("whatsapp_cloud", dict(token="cl-token-x", instance="12345")),
    ],
)
def test_every_provider_channel_fails_closed_when_a_secret_is_required(kind, extra):
    """The family property, not a patch on one channel: whatever PROVIDER webhook a host builds
    through the factory, asking for a secret and configuring none must never verify an unsigned
    request. Each kind gets the minimum config its constructor demands, so a missing-credential
    error can never be mistaken for the security property under test."""
    ch = create_channel(kind, ChannelConfig(require_secret=True, **extra))
    assert ch.verify(headers={}, body=b"") is False


def test_factory_web_forwards_an_EXPLICIT_require_secret():
    """The security fix the web special-case must keep: a caller that ASKS for web verification
    through create_channel() and configures no secret gets a CLOSED door, never an open one."""
    assert create_channel("web", ChannelConfig(require_secret=True)).verify(headers={}, body=b"") is False
    # and an explicit False opens a provider (the opt-out), the unset default keeps it closed
    assert create_channel("telegram", ChannelConfig(token="t", require_secret=False)).verify(headers={}, body=b"") is True
