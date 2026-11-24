"""Integration: EvolutionChannel sends a WhatsApp mention over a REAL socket.

The unit twins swap httpx for a fake; here a real ``http.server`` on loopback stands in for the
Evolution instance and records the body it actually received, so the whole stack runs for real —
``build_async_client``, httpx request construction and JSON encoding, a TCP connection to
``127.0.0.1``, and the ``sendText`` body with its ``mentioned`` array. No network leaves the box.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cogno_gateway import ChannelConfig, EvolutionChannel, Mention, OutboundMessage

# Synthetic ids — invented phone JIDs, never a real number.
ANA = "5511900000001@s.whatsapp.net"
BRUNO = "5511900000002@s.whatsapp.net"


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
