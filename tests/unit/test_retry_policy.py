"""Outbound retry policy for the write-safe WhatsApp channels (Evolution, Cloud).

Strict rule: retry ONLY where nothing provably reached the provider — a connection failure, a
429 (short Retry-After honoured), a 502/503/504 — and NEVER a read timeout. Telegram keeps its
own, deliberate read-timeout retry (a chat channel prefers a duplicate to a silent loss); the
last test here is the control that proves it was left untouched.
"""

import httpx
import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    OutboundMessage,
    TelegramChannel,
    WhatsAppCloudChannel,
)
from cogno_gateway import retry
from cogno_gateway.retry import MAX_ATTEMPTS, _retry_wait, post_with_retry
from tests.conftest import FakeResponse, Script

# asyncio_mode = "auto" (pyproject) runs the async tests without a per-test marker.

_REQ = httpx.Request("POST", "https://provider.example/send")


def _status(code: int, headers=None) -> httpx.HTTPStatusError:
    resp = httpx.Response(code, headers=headers or {}, request=_REQ)
    return httpx.HTTPStatusError(f"HTTP {code}", request=_REQ, response=resp)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    """Keep the backoff from spending real wall-clock time in the suite."""
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(retry, "_sleep", _noop)
    # Telegram has its own backoff constant; zero it so the control test stays fast.
    monkeypatch.setattr("cogno_gateway.telegram._RETRY_BACKOFF_SECONDS", 0)


# ── _retry_wait, per error CLASS (pure) ──────────────────────────────────────
@pytest.mark.parametrize("exc", [
    httpx.ConnectError("no route"),
    httpx.ConnectTimeout("slow connect"),
    httpx.PoolTimeout("pool full"),
])
def test_connection_failures_are_retried(exc):
    assert _retry_wait(exc, 1) == 0.5      # nothing left the box → safe to retry


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("no answer"),        # the one that must NOT repeat (would duplicate)
    httpx.WriteTimeout("mid-write"),
    httpx.WriteError("dropped while writing"),
    _status(400), _status(404), _status(500),
])
def test_ambiguous_or_final_errors_are_not_retried(exc):
    assert _retry_wait(exc, 1) is None


@pytest.mark.parametrize("code", [502, 503, 504])
def test_gateway_statuses_are_retried(code):
    assert _retry_wait(_status(code), 1) == 0.5


def test_429_honours_a_short_retry_after():
    assert _retry_wait(_status(429, {"Retry-After": "2"}), 1) == 2.0


def test_429_without_retry_after_uses_backoff():
    assert _retry_wait(_status(429), 1) == 0.5


def test_429_with_a_long_retry_after_is_not_waited_out():
    # Longer than MAX_RETRY_AFTER (5 s): don't hang the turn — fail with the reason.
    assert _retry_wait(_status(429, {"Retry-After": "30"}), 1) is None


def test_backoff_grows_then_stops_at_max_attempts():
    assert _retry_wait(httpx.ConnectError("x"), 1) == 0.5
    assert _retry_wait(httpx.ConnectError("x"), 2) == 1.0


# ── post_with_retry behaviour ────────────────────────────────────────────────
async def test_post_with_retry_recovers_after_a_connection_failure(fake_httpx):
    fake_httpx.routes = {"/send": Script(httpx.ConnectError("x"), FakeResponse({"ok": True}))}
    client = __import__("httpx").AsyncClient()
    resp = await post_with_retry("whatsapp", client, "https://p/send")
    assert resp.json() == {"ok": True}
    assert len(fake_httpx.calls) == 2


async def test_post_with_retry_gives_up_after_max_attempts(fake_httpx):
    fake_httpx.routes = {"/send": httpx.ConnectError("always")}
    client = __import__("httpx").AsyncClient()
    with pytest.raises(httpx.ConnectError):
        await post_with_retry("whatsapp", client, "https://p/send")
    assert len(fake_httpx.calls) == MAX_ATTEMPTS      # exactly 3, never more


async def test_post_with_retry_does_not_repeat_a_read_timeout(fake_httpx):
    fake_httpx.routes = {"/send": httpx.ReadTimeout("no answer")}
    client = __import__("httpx").AsyncClient()
    with pytest.raises(httpx.ReadTimeout):
        await post_with_retry("whatsapp", client, "https://p/send")
    assert len(fake_httpx.calls) == 1                 # one try, no duplicate


# ── per CHANNEL ──────────────────────────────────────────────────────────────
def _evo():
    return EvolutionChannel(ChannelConfig(base_url="http://evo:8080/", token="K", instance="i1"))


def _cloud():
    return WhatsAppCloudChannel(ChannelConfig(token="ACCESS", instance="PHONE"))


def _tg():
    return TelegramChannel(ChannelConfig(token="123:SEGREDO-INVENTADO"))


async def test_evolution_retries_a_connection_failure(fake_httpx):
    fake_httpx.routes = {"sendText": Script(httpx.ConnectError("x"),
                                            FakeResponse({"key": {"id": "OUT1"}}))}
    res = await _evo().send("5511999@s.whatsapp.net", OutboundMessage(text="hi"))
    assert res.ok and res.message_ids == ["OUT1"]
    assert len([c for c in fake_httpx.calls if "sendText" in c["url"]]) == 2


async def test_evolution_does_not_retry_a_read_timeout(fake_httpx):
    fake_httpx.routes = {"sendText": httpx.ReadTimeout("no answer")}
    res = await _evo().send("5511999@s.whatsapp.net", OutboundMessage(text="hi"))
    assert res.ok is False
    assert len([c for c in fake_httpx.calls if "sendText" in c["url"]]) == 1   # no duplicate


async def test_cloud_retries_a_gateway_error(fake_httpx):
    fake_httpx.routes = {"/messages": Script(FakeResponse(status=503),
                                             FakeResponse({"messages": [{"id": "C1"}]}))}
    res = await _cloud().send("5511", OutboundMessage(text="olá"))
    assert res.ok and res.message_ids == ["C1"]
    assert len([c for c in fake_httpx.calls if "/messages" in c["url"]]) == 2


async def test_cloud_does_not_retry_a_read_timeout(fake_httpx):
    fake_httpx.routes = {"/messages": httpx.ReadTimeout("no answer")}
    res = await _cloud().send("5511", OutboundMessage(text="olá"))
    assert res.ok is False
    assert len([c for c in fake_httpx.calls if "/messages" in c["url"]]) == 1


async def test_telegram_STILL_retries_a_read_timeout(fake_httpx):
    """Control: Telegram's measured read-timeout retry is NOT inverted by this change — a chat
    channel still prefers a possible duplicate to a silently lost reply."""
    fake_httpx.routes = {"sendMessage": Script(httpx.ReadTimeout("lost reply"),
                                               FakeResponse({"result": {"message_id": 7}}))}
    res = await _tg().send("42", OutboundMessage(text="hi"))
    assert res.ok
    assert len([c for c in fake_httpx.calls if "sendMessage" in c["url"]]) == 2   # it retried
