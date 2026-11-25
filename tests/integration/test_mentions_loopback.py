"""Integration: EvolutionChannel and TelegramChannel send a mention over a REAL socket.

The unit twins swap httpx for a fake; here a real ``http.server`` on loopback stands in for the
provider and records the body it actually received, so the whole stack runs for real —
``build_async_client``, httpx request construction and JSON encoding, a TCP connection to
``127.0.0.1``, and the send body with its mention payload (Evolution's ``mentioned`` array, or
Telegram's ``text_mention`` ``entities``). No network leaves the box.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import cogno_gateway.telegram as tg
from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    Mention,
    OutboundMessage,
    TelegramChannel,
)

# Synthetic ids — invented phone JIDs, never a real number.
ANA = "5511900000001@s.whatsapp.net"
BRUNO = "5511900000002@s.whatsapp.net"
# Synthetic Telegram numeric ids — invented, never a real account.
TG_ANA = "100000001"
TG_BRUNO = "100000002"


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's required name
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        type(self).bodies.append({"path": self.path, "json": json.loads(raw or b"{}")})
        payload = json.dumps({"key": {"id": "SRV1"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the handler's stderr logging
        return


@pytest.fixture
def evolution_server():
    """A loopback HTTP server that answers like Evolution and records every POST body."""
    _Recorder.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", _Recorder.bodies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_mentioned_rides_the_real_sendText_body(evolution_server):
    base_url, bodies = evolution_server
    ch = EvolutionChannel(ChannelConfig(base_url=base_url, token="APIKEY", instance="inst1"))

    tok = ch.mention_token(ANA, "Ana")
    res = await ch.send("123@g.us",
                        OutboundMessage(text=f"{tok} your slot is confirmed",
                                        mentions=[Mention(ANA, "Ana")]))

    assert res.ok is True and res.message_ids == ["SRV1"]
    sends = [b for b in bodies if "sendText" in b["path"]]
    assert len(sends) == 1
    body = sends[0]["json"]
    assert body["text"] == "@5511900000001 your slot is confirmed"
    assert body["mentioned"] == [ANA]
    assert "mentionsEveryOne" not in body


async def test_absent_token_sends_no_mentioned_over_the_wire(evolution_server):
    base_url, bodies = evolution_server
    ch = EvolutionChannel(ChannelConfig(base_url=base_url, token="APIKEY", instance="inst1"))

    # Bruno's token is placed; Ana's is not → only Bruno is mentioned on the wire.
    tok = ch.mention_token(BRUNO, "Bruno")
    res = await ch.send("123@g.us",
                        OutboundMessage(text=f"{tok} welcome",
                                        mentions=[Mention(ANA, "Ana"), Mention(BRUNO, "Bruno")]))

    assert res.ok is True
    body = [b for b in bodies if "sendText" in b["path"]][0]["json"]
    assert body["mentioned"] == [BRUNO]


class _TelegramRecorder(BaseHTTPRequestHandler):
    """Answers like the Telegram Bot API (``{"ok": true, "result": {...}}``) and records bodies."""

    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's required name
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        type(self).bodies.append({"path": self.path, "json": json.loads(raw or b"{}")})
        payload = json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the handler's stderr logging
        return


@pytest.fixture
def telegram_server(monkeypatch):
    """A loopback HTTP server that answers like the Bot API and records every POST body.

    The Bot API base lives in ``cogno_gateway.telegram._API``; point it at the loopback for the
    duration of the test so ``send`` builds a real ``…/bot<token>/sendMessage`` URL against it."""
    _TelegramRecorder.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TelegramRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    monkeypatch.setattr(tg, "_API", f"http://{host}:{port}")
    try:
        yield _TelegramRecorder.bodies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_telegram_text_mention_entity_rides_the_real_sendmessage_body(telegram_server):
    bodies = telegram_server
    ch = TelegramChannel(ChannelConfig(token="T"))

    tok = ch.mention_token(TG_ANA, "José")       # the token is the label
    res = await ch.send("42", OutboundMessage(text=f"Oi {tok}, pronto",
                                              mentions=[Mention(TG_ANA, "José")]))

    assert res.ok is True and res.message_ids == ["7"]
    sends = [b for b in bodies if "sendMessage" in b["path"]]
    assert len(sends) == 1
    body = sends[0]["json"]
    assert body["text"] == "Oi José, pronto"
    assert body["entities"] == [
        {"type": "text_mention", "offset": 3, "length": 4, "user": {"id": 100000001}}]
    assert "parse_mode" not in body


async def test_telegram_absent_token_sends_no_entity_over_the_wire(telegram_server):
    bodies = telegram_server
    ch = TelegramChannel(ChannelConfig(token="T"))

    # Bruno's name is placed; Ana's is not → only Bruno gets an entity on the wire.
    tok = ch.mention_token(TG_BRUNO, "Bruno")
    res = await ch.send("42", OutboundMessage(
        text=f"{tok}, bem-vindo",
        mentions=[Mention(TG_ANA, "Ana"), Mention(TG_BRUNO, "Bruno")]))

    assert res.ok is True
    body = [b for b in bodies if "sendMessage" in b["path"]][0]["json"]
    assert body["entities"] == [
        {"type": "text_mention", "offset": 0, "length": 5, "user": {"id": 100000002}}]
