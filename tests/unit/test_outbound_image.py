"""An outbound JPEG or PNG goes out as a VISIBLE IMAGE, not as a document attachment.

Each of the three provider adapters (Evolution/WhatsApp, Telegram, WhatsApp Cloud) used to send
every ``OutboundMessage.media`` item as a document. A photo a host hands back — a chart, a receipt,
a floor plan — then arrived as a file to download instead of showing inline in the chat. These
tests drive the real ``send`` with ``httpx`` mocked (no network, nothing is ever sent) and read the
request body each adapter built.

G1–G3 measure the behaviour through ``send`` ALONE and never import ``image_mime``, so the
red-on-base is a behavioural one (the body still says ``document``), not an ``ImportError``. The
controls pin the document path unchanged, byte for byte, and WebP staying a document.

URLs are synthetic (``*.invalid``) and captions carry an invented, rare name so no real figure can
hide in a fixture.
"""

import pytest

from cogno_gateway import (
    ChannelConfig,
    EvolutionChannel,
    MediaRef,
    OutboundMessage,
    TelegramChannel,
    WhatsAppCloudChannel,
)
from tests.conftest import FakeResponse, body_of

# ── adapters under test, one recipient each ──────────────────────────────────
_EVO_CFG = ChannelConfig(base_url="http://evo:8080/", token="APIKEY", instance="inst1")
_TG_CFG = ChannelConfig(token="BOT123")
_CLOUD_CFG = ChannelConfig(token="ACCESS", instance="PHONE_ID", secret="appsecret")

_EVO_TO = "5511@s.whatsapp.net"
_TG_TO = "42"
_CLOUD_TO = "5511999"

_URL = "https://media.example.invalid/chart.png"
_CAP = "Belmira Quostral Vendrim's quarterly chart"


def _evo():
    return EvolutionChannel(_EVO_CFG)


def _tg():
    return TelegramChannel(_TG_CFG)


def _cloud():
    return WhatsAppCloudChannel(_CLOUD_CFG)


def _calls(fake, substr):
    return [c for c in fake.calls if substr in c["url"]]


# ── G1: Evolution sends an image as sendMedia mediatype=image ────────────────
# Only the ``mediatype`` changes: the body keeps the SAME four keys as a document send, so no
# ``mimetype`` and no ``fileName`` are added — even when the MediaRef carries a filename. The
# server reads the picture's type from the bytes, and a filename would make it re-derive it.
async def test_g1_evolution_image_goes_as_image_mediatype(fake_httpx):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "IMG1"}})}
    await _evo().send(_EVO_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png", caption=_CAP)]))
    media = _calls(fake_httpx, "sendMedia")
    assert len(media) == 1
    body = body_of(media[0])
    assert body == {"number": "5511", "media": _URL, "mediatype": "image", "caption": _CAP}


async def test_g1_evolution_image_with_filename_adds_no_extra_keys(fake_httpx):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "IMG1"}})}
    await _evo().send(_EVO_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png", caption=_CAP, filename="chart.png")]))
    body = body_of(_calls(fake_httpx, "sendMedia")[0])
    assert body == {"number": "5511", "media": _URL, "mediatype": "image", "caption": _CAP}
    assert "mimetype" not in body
    assert "fileName" not in body


# ── G2: Telegram sends an image via sendPhoto, never sendDocument ────────────
async def test_g2_telegram_image_uses_sendphoto_not_senddocument(fake_httpx):
    await _tg().send(_TG_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png", caption=_CAP)]))
    photos = _calls(fake_httpx, "sendPhoto")
    assert len(photos) == 1
    body = body_of(photos[0])
    assert body["photo"] == _URL
    assert body["caption"] == _CAP
    assert _calls(fake_httpx, "sendDocument") == []


# ── G3: Cloud sends an image as type=image ───────────────────────────────────
async def test_g3_cloud_image_goes_as_image_type(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "IMG1"}]})}
    await _cloud().send(_CLOUD_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png", caption=_CAP)]))
    sent = _calls(fake_httpx, "/messages")
    assert len(sent) == 1
    body = body_of(sent[0])
    assert body["type"] == "image"
    assert body["image"] == {"link": _URL, "caption": _CAP}
    assert "document" not in body


# ── G4: the MIME is normalized (case, parameters) on every adapter ───────────
# The normalization (case folding, parameter stripping) still DECIDES image vs document; the
# Evolution body carries no normalized ``mimetype`` of its own.
@pytest.mark.parametrize("mime", ["Image/JPEG", "image/png; charset=binary"])
async def test_g4_evolution_normalizes_mime(fake_httpx, mime):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "IMG1"}})}
    await _evo().send(_EVO_TO, OutboundMessage(media=[MediaRef(url=_URL, mime=mime)]))
    assert body_of(_calls(fake_httpx, "sendMedia")[0])["mediatype"] == "image"


@pytest.mark.parametrize("mime", ["Image/JPEG", "image/png; charset=binary"])
async def test_g4_telegram_normalizes_mime(fake_httpx, mime):
    await _tg().send(_TG_TO, OutboundMessage(media=[MediaRef(url=_URL, mime=mime)]))
    assert len(_calls(fake_httpx, "sendPhoto")) == 1
    assert _calls(fake_httpx, "sendDocument") == []


@pytest.mark.parametrize("mime", ["Image/JPEG", "image/png; charset=binary"])
async def test_g4_cloud_normalizes_mime(fake_httpx, mime):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "IMG1"}]})}
    await _cloud().send(_CLOUD_TO, OutboundMessage(media=[MediaRef(url=_URL, mime=mime)]))
    assert body_of(_calls(fake_httpx, "/messages")[0])["type"] == "image"


# ── G5: a provider failure on the image path is reported ─────────────────────
async def test_g5_evolution_image_failure_is_reported(fake_httpx):
    fake_httpx.routes = {"/sendMedia/": FakeResponse(status=500)}
    res = await _evo().send(_EVO_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png")]))
    assert res.ok is False
    assert res.error


async def test_g5_telegram_image_failure_is_reported(fake_httpx):
    fake_httpx.routes = {"/sendPhoto": FakeResponse(status=400)}
    res = await _tg().send(_TG_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png")]))
    assert res.ok is False
    assert res.error


async def test_g5_cloud_image_failure_is_reported(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse(status=500)}
    res = await _cloud().send(_CLOUD_TO, OutboundMessage(
        media=[MediaRef(url=_URL, mime="image/png")]))
    assert res.ok is False
    assert res.error


# ── C1: the DOCUMENT body is byte-identical to the base (keys and order) ─────
@pytest.mark.parametrize("mime", ["", "application/pdf"])
async def test_c1_evolution_document_body_unchanged(fake_httpx, mime):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "doc-1"}})}
    await _evo().send(_EVO_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/doc.pdf", mime=mime, caption=_CAP)]))
    body = body_of(_calls(fake_httpx, "sendMedia")[0])
    expected = {"number": "5511", "media": "https://media.example.invalid/doc.pdf",
                "mediatype": "document", "caption": _CAP}
    assert body == expected
    assert list(body.keys()) == list(expected.keys())


@pytest.mark.parametrize("mime", ["", "application/pdf"])
async def test_c1_telegram_document_body_unchanged(fake_httpx, mime):
    await _tg().send(_TG_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/doc.pdf", mime=mime, caption=_CAP)]))
    assert _calls(fake_httpx, "sendPhoto") == []
    body = body_of(_calls(fake_httpx, "sendDocument")[0])
    expected = {"chat_id": _TG_TO, "document": "https://media.example.invalid/doc.pdf"}
    assert body == expected
    assert list(body.keys()) == list(expected.keys())


@pytest.mark.parametrize("mime", ["", "application/pdf"])
async def test_c1_cloud_document_body_unchanged(fake_httpx, mime):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "doc-1"}]})}
    await _cloud().send(_CLOUD_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/doc.pdf", mime=mime, caption=_CAP)]))
    body = body_of(_calls(fake_httpx, "/messages")[0])
    expected = {"messaging_product": "whatsapp", "to": _CLOUD_TO, "type": "document",
                "document": {"link": "https://media.example.invalid/doc.pdf", "caption": _CAP}}
    assert body == expected
    assert list(body.keys()) == list(expected.keys())


# ── C2: WebP stays a document on every adapter ───────────────────────────────
async def test_c2_evolution_webp_stays_document(fake_httpx):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "WEBPID"}})}
    await _evo().send(_EVO_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/x.webp", mime="image/webp")]))
    body = body_of(_calls(fake_httpx, "sendMedia")[0])
    assert body["mediatype"] == "document"
    assert "mimetype" not in body


async def test_c2_telegram_webp_stays_document(fake_httpx):
    await _tg().send(_TG_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/x.webp", mime="image/webp")]))
    assert _calls(fake_httpx, "sendPhoto") == []
    assert len(_calls(fake_httpx, "sendDocument")) == 1


async def test_c2_cloud_webp_stays_document(fake_httpx):
    fake_httpx.routes = {"/messages": FakeResponse({"messages": [{"id": "WEBPID"}]})}
    await _cloud().send(_CLOUD_TO, OutboundMessage(
        media=[MediaRef(url="https://media.example.invalid/x.webp", mime="image/webp")]))
    assert body_of(_calls(fake_httpx, "/messages")[0])["type"] == "document"


# ── C3: a mixed [png, pdf] batch → image then document, both ids kept ────────
_PNG = MediaRef(url="https://media.example.invalid/a.png", mime="image/png")
_PDF = MediaRef(url="https://media.example.invalid/b.pdf", mime="application/pdf")


async def test_c3_evolution_mixed_image_then_document(fake_httpx):
    fake_httpx.routes = {"/sendMedia/": FakeResponse({"key": {"id": "M1"}})}
    res = await _evo().send(_EVO_TO, OutboundMessage(media=[_PNG, _PDF]))
    media = _calls(fake_httpx, "sendMedia")
    assert len(media) == 2
    assert body_of(media[0])["mediatype"] == "image"
    assert body_of(media[0])["media"] == _PNG.url
    assert body_of(media[1])["mediatype"] == "document"
    assert body_of(media[1])["media"] == _PDF.url
    assert res.message_ids == ["M1", "M1"]


async def test_c3_telegram_mixed_image_then_document(fake_httpx):
    fake_httpx.routes = {
        "/sendPhoto": FakeResponse({"result": {"message_id": 101}}),
        "/sendDocument": FakeResponse({"result": {"message_id": 102}}),
    }
    res = await _tg().send(_TG_TO, OutboundMessage(media=[_PNG, _PDF]))
    assert len(_calls(fake_httpx, "sendPhoto")) == 1
    assert len(_calls(fake_httpx, "sendDocument")) == 1
    assert body_of(_calls(fake_httpx, "sendPhoto")[0])["photo"] == _PNG.url
    assert body_of(_calls(fake_httpx, "sendDocument")[0])["document"] == _PDF.url
    assert res.message_ids == ["101", "102"]


async def test_c3_cloud_mixed_image_then_document(fake_httpx):
    from tests.conftest import Script
    fake_httpx.routes = {"/messages": Script(
        FakeResponse({"messages": [{"id": "I1"}]}),
        FakeResponse({"messages": [{"id": "doc-2"}]}),
    )}
    res = await _cloud().send(_CLOUD_TO, OutboundMessage(media=[_PNG, _PDF]))
    sent = _calls(fake_httpx, "/messages")
    assert len(sent) == 2
    assert body_of(sent[0])["type"] == "image"
    assert body_of(sent[0])["image"]["link"] == _PNG.url
    assert body_of(sent[1])["type"] == "document"
    assert body_of(sent[1])["document"]["link"] == _PDF.url
    assert res.message_ids == ["I1", "doc-2"]
