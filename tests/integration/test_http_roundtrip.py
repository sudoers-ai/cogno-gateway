"""
Integration: real httpx request/response round-trip via ``httpx.MockTransport``.

The unit suite swaps in a hand-rolled ``FakeAsyncClient`` that *replaces* httpx
entirely, so the real serialization layer never runs — actual ``Request``
construction, JSON encoding, header application, URL joining and
``raise_for_status``. Here we keep the **real** ``httpx.AsyncClient`` and only swap
its transport for an in-process ``MockTransport``, so those layers are exercised
end to end (no network). This catches wiring bugs the full-client fake cannot.
"""

import base64
import json

import httpx
import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    MediaRef,
    OutboundMessage,
    WhatsAppCloudChannel,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


@pytest.fixture
def mock_http(monkeypatch):
    """Install a responder; returns the recorder capturing the real httpx Requests."""

    def install(responder) -> _Recorder:
        rec = _Recorder(responder)

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(rec))
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return rec

    return install


CLOUD_CFG = ChannelConfig(token="ACCESS", instance="PHONE_ID", secret="appsecret",
                          extra={"verify_token": "vtok"})
EVO_CFG = ChannelConfig(base_url="https://evo.local", token="APIKEY", instance="inst1",
                        secret="s")


# ── WhatsApp Cloud ───────────────────────────────────────────────────────
async def test_cloud_send_text_serializes_real_request(mock_http):
    rec = mock_http(lambda req: httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))

    res = await WhatsAppCloudChannel(CLOUD_CFG).send("5511", OutboundMessage(text="olá"))

    assert res.ok is True and res.message_ids == ["wamid.X"]
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v21.0/PHONE_ID/messages"
    assert req.headers["authorization"] == "Bearer ACCESS"
    # the body really went through httpx JSON serialization (UTF-8 preserved)
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp", "to": "5511", "type": "text",
        "text": {"body": "olá"},
    }


async def test_cloud_http_error_becomes_sendresult_false(mock_http):
    # 401 → real raise_for_status → HTTPStatusError → caught → SendResult(ok=False)
    mock_http(lambda req: httpx.Response(401, json={"error": {"message": "bad token"}}))
    res = await WhatsAppCloudChannel(CLOUD_CFG).send("5511", OutboundMessage(text="hi"))
    assert res.ok is False and res.error


async def test_cloud_fetch_media_two_step_round_trip(mock_http):
    def responder(req: httpx.Request) -> httpx.Response:
        if req.url.host == "graph.facebook.com":          # step 1: media-id → url
            return httpx.Response(200, json={"url": "https://lh3.media/abc"})
        return httpx.Response(200, content=b"JPEGBYTES")  # step 2: download

    rec = mock_http(responder)
    data = await WhatsAppCloudChannel(CLOUD_CFG).fetch_media(MediaRef(ref="MEDIA_ID"))

    assert data == b"JPEGBYTES"
    assert rec.requests[0].url.path == "/v21.0/MEDIA_ID"
    # the bearer token travels to the (off-Graph) media URL too
    assert rec.requests[1].headers["authorization"] == "Bearer ACCESS"


# ── Evolution (unofficial WhatsApp) ──────────────────────────────────────
async def test_evolution_send_text_serializes_real_request(mock_http):
    rec = mock_http(lambda req: httpx.Response(200, json={"key": {"id": "EVO1"}}))

    res = await EvolutionChannel(EVO_CFG).send(
        "5511@s.whatsapp.net", OutboundMessage(text="oi")
    )

    assert res.ok is True and "EVO1" in res.message_ids
    req = rec.requests[0]
    assert req.url.path == "/message/sendText/inst1"
    assert req.headers["apikey"] == "APIKEY"
    assert json.loads(req.content) == {"number": "5511", "text": "oi"}


async def test_evolution_fetch_media_base64_decodes(mock_http):
    payload = base64.b64encode(b"AUDIOBYTES").decode("ascii")
    rec = mock_http(lambda req: httpx.Response(200, json={"base64": payload}))

    data = await EvolutionChannel(EVO_CFG).fetch_media(MediaRef(ref="msgid"))

    assert data == b"AUDIOBYTES"
    assert rec.requests[0].url.path == "/chat/getBase64FromMediaMessage/inst1"
    assert rec.requests[0].headers["apikey"] == "APIKEY"
