"""A failed Telegram send never reports the bot token: not in a log line, not in ``SendResult.error``.

The token sits in the PATH of every Bot API URL (``/bot<TOKEN>/sendMessage``), and httpx writes the
request URL into the text of the ``HTTPStatusError`` that ``raise_for_status`` raises. For an answer
with no body (a redirect, a proxy's 5xx) that text was the whole error on 0.1.2: it went into the
``send_failed`` WARNING line and into ``SendResult.error``, which a host logs again.

These tests keep the REAL ``httpx.AsyncClient`` and swap only its transport for an in-process
``httpx.MockTransport`` (no network). The suite's ``FakeAsyncClient`` raises an error whose text is
``"HTTP 500"``, so it could never show the URL, which is how the leak went unseen.

The sentinel is a planted token and the check is byte-level: no 6-character window of the token,
in any case, appears in any record the package logs or in ``SendResult.error``. Every sentinel first
proves that the token really was in the URL of the failing request, so "nothing leaked" can never
be "nothing was sent".
"""

import logging
from urllib.parse import quote

import httpx
import pytest

from cogno_gateway import ChannelConfig, MediaRef, OutboundMessage, Reaction, TelegramChannel

_REAL_ASYNC_CLIENT = httpx.AsyncClient

#: Planted, never a real credential: the shape of a Bot API token (a numeric bot id, a colon, 35
#: characters), so the percent-encoded form has the one escaped character a real token has.
TOKEN = "7310468259:AAHplantedSENTINELqZr4w9Jm2TbHn8sYd"
WINDOW = 6
_FORMAT = logging.Formatter("%(name)s %(levelname)s %(message)s")


def _leaks(text: str) -> list[str]:
    """Every 6-character window of the token found in ``text``, case ignored."""
    folded = text.lower()
    windows = {TOKEN[i:i + WINDOW] for i in range(len(TOKEN) - WINDOW + 1)}
    return sorted(w for w in windows if w.lower() in folded)


class _Backoff:
    """Stands in for the module's ``asyncio`` so the retry's pause is instantaneous."""

    async def sleep(self, seconds):
        return None


@pytest.fixture
def telegram_answers(monkeypatch):
    """Install a responder for every Bot API call; returns the list of requests it saw."""
    from cogno_gateway import telegram as tg
    monkeypatch.setattr(tg, "asyncio", _Backoff())

    def install(responder):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return responder(request)

        def factory(*args, **kwargs):
            # Assignment, not ``setdefault``: ``build_async_client`` always supplies a transport.
            kwargs["transport"] = httpx.MockTransport(handler)
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return seen

    return install


async def _send(telegram_answers, caplog, responder, message=None):
    seen = telegram_answers(responder)
    with caplog.at_level(logging.DEBUG, logger="cogno_gateway"):
        res = await TelegramChannel(ChannelConfig(token=TOKEN)).send(
            "42", message or OutboundMessage(text="oi"))
    lines = [_FORMAT.format(r) for r in caplog.records]
    return res, seen, lines


def _event(lines, event):
    return [line.split(" ", 2)[2] for line in lines if f"event={event} " in line]


# ── the failing answers: each one carried the token on 0.1.2 ──────────────────

def _bodiless_502(request):
    return httpx.Response(502)


def _bodiless_redirect(request):
    # the Location of a redirect repeats the path, token included
    return httpx.Response(302, headers={
        "location": str(request.url).replace("api.telegram.org", "api.example.invalid")})


def _html_echoing_the_url(request):
    return httpx.Response(500, text=f"<html><body>upstream error for {request.url}</body></html>")


def _echo_percent_encoded(request):
    return httpx.Response(500, text=f"upstream error for /bot{quote(TOKEN, safe='')}/sendMessage")


def _echo_percent_encoded_lowercase(request):
    encoded = quote(TOKEN, safe="").replace("%3A", "%3a")
    return httpx.Response(500, text=f"upstream error for /bot{encoded}/sendMessage")


def _description_echoing_the_token(request):
    return httpx.Response(401, json={"ok": False, "description": f"Unauthorized: {TOKEN} revoked"})


def _transport_error_naming_the_url(request):
    # Constructed: httpx does not put the URL in a transport error's message today. The test plants
    # it there so the retry line and the field are held to the rule by mechanism, not by httpx's
    # current wording.
    raise httpx.ConnectError(f"connection to {request.url} failed", request=request)


SENTINELS = {
    "bodiless 502": (_bodiless_502, "502 HTTPStatusError"),
    "bodiless 302": (_bodiless_redirect, "302 HTTPStatusError"),
    "html echo": (_html_echoing_the_url,
                  "500 <html><body>upstream error for "
                  "https://api.telegram.org/bot[token]/sendMessage</body></html>"),
    "percent-encoded echo": (_echo_percent_encoded,
                             "500 upstream error for /bot[token]/sendMessage"),
    "percent-encoded echo, lower case": (_echo_percent_encoded_lowercase,
                                         "500 upstream error for /bot[token]/sendMessage"),
    "description echo": (_description_echoing_the_token, "401 Unauthorized: [token] revoked"),
    "transport message": (_transport_error_naming_the_url,
                          "connection to https://api.telegram.org/bot[token]/sendMessage failed"),
}


@pytest.mark.parametrize("case", list(SENTINELS))
async def test_no_byte_of_the_token_leaves_a_failed_send(telegram_answers, caplog, case):
    responder, expected_error = SENTINELS[case]
    res, seen, lines = await _send(telegram_answers, caplog, responder)

    # armed: the failing request really carried the token, and the failure was really logged
    assert seen and TOKEN in str(seen[0].url)
    assert res.ok is False
    assert _event(lines, "send_failed"), "the failure must still be logged"

    assert _leaks(res.error) == []
    assert [line for line in lines if _leaks(line)] == []
    assert res.error == expected_error


async def test_the_failure_line_is_the_class_and_the_status(telegram_answers, caplog):
    _, _, lines = await _send(telegram_answers, caplog, _html_echoing_the_url)
    assert _event(lines, "send_failed") == [
        "channel=telegram event=send_failed sent=0 error=HTTPStatusError status=500"]


async def test_a_transport_failure_logs_the_class_on_both_lines(telegram_answers, caplog):
    _, seen, lines = await _send(telegram_answers, caplog, _transport_error_naming_the_url)
    assert len(seen) == 2, "a transport failure is retried once"
    assert _event(lines, "send_retry") == [
        "channel=telegram event=send_retry attempt=2 after=ConnectError"]
    assert _event(lines, "send_failed") == [
        "channel=telegram event=send_failed sent=0 error=ConnectError status=none"]


@pytest.mark.parametrize("message,endpoint", [
    (OutboundMessage(text="oi"), "sendMessage"),
    (OutboundMessage(reaction=Reaction("👍", "7")), "setMessageReaction"),
    (OutboundMessage(audio=b"OPUS", audio_format="ogg"), "sendVoice"),
    (OutboundMessage(media=[MediaRef(url="http://x.invalid/a.pdf")]), "sendDocument"),
])
async def test_every_bot_api_call_fails_without_the_token(telegram_answers, caplog, message,
                                                          endpoint):
    res, seen, lines = await _send(telegram_answers, caplog, _bodiless_502, message)

    assert seen and seen[0].url.path == f"/bot{TOKEN}/{endpoint}"
    assert res.error == "502 HTTPStatusError"
    assert [line for line in lines if _leaks(line)] == []


# ── controls: an error text that carries no token keeps every useful byte ─────

CONTROLS = {
    "telegram description": (
        lambda r: httpx.Response(400, json={"ok": False,
                                            "description": "Bad Request: chat not found"}),
        "400 Bad Request: chat not found"),
    # the word "bot" is not the token: the mask goes by the token's value, not by a pattern
    "description naming the bot": (
        lambda r: httpx.Response(403, json={"ok": False,
                                            "description": "Forbidden: bot was blocked by the user"}),
        "403 Forbidden: bot was blocked by the user"),
    "plain-text body": (
        lambda r: httpx.Response(503, text="upstream overloaded, retry later"),
        "503 upstream overloaded, retry later"),
}


@pytest.mark.parametrize("case", list(CONTROLS))
async def test_an_error_text_without_the_token_is_kept_whole(telegram_answers, caplog, case):
    responder, expected_error = CONTROLS[case]
    res, _, _ = await _send(telegram_answers, caplog, responder)
    assert res.error == expected_error


async def test_a_read_timeout_is_still_named_by_its_class(telegram_answers, caplog):
    def timeout(request):
        raise httpx.ReadTimeout("", request=request)

    res, seen, lines = await _send(telegram_answers, caplog, timeout)
    assert len(seen) == 2
    assert res.error == "ReadTimeout"
    assert _event(lines, "send_failed") == [
        "channel=telegram event=send_failed sent=0 error=ReadTimeout status=none"]
