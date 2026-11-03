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
            # Assignment and not ``setdefault``: since the adapters build their client through
            # ``cogno_gateway.net.build_async_client``, a transport is ALWAYS supplied — the
            # family-fallback one. A ``setdefault`` here would quietly lose to it and this suite
            # would leave the box looking for the real provider.
            kwargs["transport"] = httpx.MockTransport(rec)
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
    assert req.url.path == "/v25.0/PHONE_ID/messages"
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
    assert rec.requests[0].url.path == "/v25.0/MEDIA_ID"
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


# ── Telegram, a group reply (0.1.4) ──────────────────────────────────────
async def test_telegram_group_reply_is_threaded_to_the_room_over_real_httpx(mock_http):
    """The parse gives ``sender`` = the room; the reply sent there with ``reply_to_message_id``
    serialises the room's negative id and ``reply_parameters`` through the real httpx layers.
    Every id and handle is invented."""
    from cogno_gateway import TelegramChannel

    rec = mock_http(lambda req: httpx.Response(200, json={"ok": True,
                                                         "result": {"message_id": 91}}))
    ch = TelegramChannel(ChannelConfig(token="7000001:SEGREDO-INVENTADO", secret="s",
                                       accept_groups=True, bot_username="@assistente_teste_bot"))
    text = "@assistente_teste_bot que horas abre?"
    inbound = ch.parse_inbound({"message": {
        "message_id": 57, "text": text,
        "chat": {"id": -1001234567890, "type": "supergroup"},
        "from": {"id": 5550001, "is_bot": False},
        "entities": [{"type": "mention", "offset": 0, "length": 21}]}})
    assert inbound.mentions_bot is True and inbound.author == "5550001"

    res = await ch.send(inbound.sender, OutboundMessage(text="Abre às 8h.",
                                                        reply_to_message_id=inbound.message_id))

    assert res.ok is True and res.message_ids == ["91"]
    req = rec.requests[0]
    assert req.url.path.endswith("/sendMessage")
    assert json.loads(req.content) == {
        "chat_id": "-1001234567890", "text": "Abre às 8h.",
        "reply_parameters": {"message_id": 57, "allow_sending_without_reply": True}}


async def test_the_registrar_subscribes_to_the_bots_membership_and_the_update_parses(mock_http):
    """0.1.6: ``setWebhook`` over the real httpx layers asks for ``my_chat_member``, and the update
    Telegram then posts (decoded from its JSON bytes, as a webhook receives it) parses to ONE
    membership event for the room. Every id and handle is invented."""
    from cogno_gateway import (MEMBERSHIP_LEFT, MessageKind, TelegramChannel,
                               TelegramWebhookRegistrar)

    rec = mock_http(lambda req: httpx.Response(200, json={"ok": True, "result": True}))
    ok, desc = await TelegramWebhookRegistrar(webhook_base="https://host.invalid").register(
        token="7000001:SEGREDO-INVENTADO", webhook_key="k1", secret="s")
    assert (ok, desc) == (True, "")
    sent = json.loads(rec.requests[0].content)
    assert "my_chat_member" in sent["allowed_updates"]

    body = json.dumps({"update_id": 3, "my_chat_member": {
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "Turma Inventada"},
        "from": {"id": 5550001, "is_bot": False}, "date": 1,
        "old_chat_member": {"user": {"id": 7000001, "is_bot": True}, "status": "member"},
        "new_chat_member": {"user": {"id": 7000001, "is_bot": True}, "status": "kicked"}}}).encode()
    ch = TelegramChannel(ChannelConfig(token="7000001:SEGREDO-INVENTADO", secret="s",
                                       accept_groups=True, bot_username="@assistente_teste_bot"))
    msg = ch.parse_inbound(json.loads(body))
    assert msg.kind is MessageKind.MEMBERSHIP and msg.membership.status == MEMBERSHIP_LEFT
    assert (msg.sender, msg.conversation_id, msg.author) == ("-1001234567890",
                                                             "-1001234567890", "")


@pytest.mark.parametrize("status,body,expected", [
    (200, {"ok": True, "result": {"id": 7000001, "can_read_all_group_messages": False}}, False),
    (200, {"ok": True, "result": {"id": 7000001, "can_read_all_group_messages": True}}, True),
    (200, {"ok": True, "result": {"id": 7000001}}, None),
    (401, {"ok": False, "description": "Unauthorized"}, None),
    (500, {}, None),
], ids=["privacy-on", "privacy-off", "field-absent", "refused", "server-error"])
async def test_the_channel_reads_the_bots_privacy_over_real_httpx(mock_http, status, body,
                                                                  expected):
    """0.1.6: ONE ``getMe`` for the channel's own bot; privacy on is ``False``, and anything the
    answer does not say is ``None`` (unknown), never ``False``. Every id is invented."""
    from cogno_gateway import TelegramChannel

    rec = mock_http(lambda req: httpx.Response(status, json=body))
    ch = TelegramChannel(ChannelConfig(token="7000001:SEGREDO-INVENTADO", secret="s"))
    assert await ch.reads_all_group_messages() is expected
    assert [r.url.path for r in rec.requests] == ["/bot7000001:SEGREDO-INVENTADO/getMe"]


async def test_the_channel_privacy_read_is_unknown_on_a_transport_error(mock_http, caplog):
    from cogno_gateway import TelegramChannel

    def _boom(req):
        raise httpx.ConnectError("refused", request=req)

    mock_http(_boom)
    ch = TelegramChannel(ChannelConfig(token="7000001:SEGREDO-INVENTADO", secret="s"))
    assert await ch.reads_all_group_messages() is None
    assert "SEGREDO-INVENTADO" not in caplog.text
