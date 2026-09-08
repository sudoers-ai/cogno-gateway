"""TelegramWebhookRegistrar — the setWebhook writer behind an admin panel's "activate".

Both directions matter: the payload Telegram must receive (url + secret_token + the same
allowed_updates every writer subscribes), and every failure coming back as ``(False, reason)`` —
an admin click surfaces reasons; it never 500s and never lies "registered".

Moved here from an application that had written it alone, because a webhook REGISTRAR belongs
beside the channel that VERIFIES what the registration causes to be sent.
"""

from __future__ import annotations

import asyncio

from cogno_gateway import (
    NullTelegramRegistrar,
    TelegramWebhookRegistrar,
    build_telegram_registrar,
)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._p = payload if payload is not None else {"ok": True}

    def json(self):
        return self._p


class _FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.calls = []
        self._resp = resp or _Resp()
        self._exc = exc

    async def get(self, url):
        self.calls.append((url, None))
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def post(self, url, *, json=None):
        self.calls.append((url, json))
        if self._exc is not None:
            raise self._exc
        return self._resp


def _reg(http, **kw):
    kw.setdefault("webhook_base", "https://pub.test")
    return TelegramWebhookRegistrar(client=http, **kw)


def test_register_sends_url_secret_and_allowed_updates():
    http = _FakeHttp()
    ok, desc = asyncio.run(_reg(http).register(
        token="123:ABC", webhook_key="telegram-acme", secret="s3cret"))
    assert ok is True and desc == ""
    url, payload = http.calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/setWebhook"
    assert payload["url"] == "https://pub.test/webhook/telegram-acme"
    assert payload["secret_token"] == "s3cret"
    # the one definition every writer must share — ``setWebhook`` REPLACES the record, so
    # two lists that drift silently change which features work
    assert payload["allowed_updates"] == ["message", "edited_message", "callback_query",
                                          "message_reaction"]


def test_register_omits_secret_token_when_secret_empty():
    """Telegram treats secret_token="" as "clear the secret" — omitting is the correct
    no-secret shape."""
    http = _FakeHttp()
    ok, _ = asyncio.run(_reg(http).register(token="1:A", webhook_key="k", secret=""))
    assert ok is True
    assert "secret_token" not in http.calls[0][1]


def test_register_fails_softly_without_public_url():
    http = _FakeHttp()
    ok, desc = asyncio.run(_reg(http, webhook_base="").register(
        token="1:A", webhook_key="k", secret="s"))
    assert ok is False and "public URL" in desc
    assert http.calls == []                       # never hit Telegram with a bad address


def test_register_fails_softly_without_token():
    ok, desc = asyncio.run(_reg(_FakeHttp()).register(token="", webhook_key="k", secret="s"))
    assert ok is False and "token" in desc


def test_register_surfaces_telegram_rejection():
    http = _FakeHttp(resp=_Resp(401, {"ok": False, "description": "Unauthorized"}))
    ok, desc = asyncio.run(_reg(http).register(token="bad:T", webhook_key="k", secret="s"))
    assert ok is False and desc == "Unauthorized"


def test_register_survives_a_transport_error():
    http = _FakeHttp(exc=RuntimeError("conn refused"))
    ok, desc = asyncio.run(_reg(http).register(token="1:A", webhook_key="k", secret="s"))
    assert ok is False and "setWebhook request failed" in desc


def test_null_registrar_reports_why():
    ok, desc = asyncio.run(NullTelegramRegistrar().register(token="1:A", webhook_key="k",
                                                            secret="s"))
    assert ok is False and "public URL" in desc


def test_build_registrar_from_env():
    real = build_telegram_registrar({"COGNO_PUBLIC_URL": "https://pub.test"})
    assert isinstance(real, TelegramWebhookRegistrar)
    assert isinstance(build_telegram_registrar({}), NullTelegramRegistrar)


# ── unregister (deleteWebhook): the other half of the lifecycle ─────────────────────────

def test_unregister_calls_delete_webhook_and_drops_the_backlog():
    """``drop_pending_updates`` matters: a message sent while the channel was off must not
    arrive minutes-to-days later as if it were new."""
    http = _FakeHttp()
    ok, desc = asyncio.run(_reg(http).unregister(token="123:ABC"))
    assert ok is True and desc == ""
    url, payload = http.calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/deleteWebhook"
    assert payload == {"drop_pending_updates": True}


def test_unregister_needs_no_public_url():
    """Taking a webhook DOWN does not require knowing our own address — and a box whose
    tunnel is dead is exactly when you want to be able to stop the deliveries."""
    http = _FakeHttp()
    ok, _ = asyncio.run(_reg(http, webhook_base="").unregister(token="123:ABC"))
    assert ok is True and http.calls


def test_unregister_without_token_fails_softly():
    http = _FakeHttp()
    ok, desc = asyncio.run(_reg(http).unregister(token=""))
    assert ok is False and "token" in desc
    assert http.calls == []


def test_unregister_surfaces_telegram_rejection():
    http = _FakeHttp(resp=_Resp(401, {"ok": False, "description": "Unauthorized"}))
    ok, desc = asyncio.run(_reg(http).unregister(token="revoked:TOK"))
    assert ok is False and desc == "Unauthorized"


def test_unregister_survives_a_transport_error():
    http = _FakeHttp(exc=RuntimeError("conn refused"))
    ok, desc = asyncio.run(_reg(http).unregister(token="123:ABC"))
    assert ok is False and "deleteWebhook request failed" in desc


def test_null_registrar_unregister_reports_why():
    ok, desc = asyncio.run(NullTelegramRegistrar().unregister(token="123:ABC"))
    assert ok is False and "public URL" in desc


def test_unregister_can_keep_the_backlog():
    """The other half of the trade-off: the caller may want Telegram to replay what it held
    (up to 24h) when the channel comes back."""
    http = _FakeHttp()
    asyncio.run(_reg(http).unregister(token="123:ABC", drop_pending=False))
    assert http.calls[0][1] == {"drop_pending_updates": False}


def test_bot_username_returns_the_handle_with_an_at():
    http = _FakeHttp(resp=_Resp(200, {"ok": True, "result": {"username": "pam_bot"}}))
    assert asyncio.run(_reg(http).bot_username(token="123:ABC")) == "@pam_bot"
    assert http.calls[0][0] == "https://api.telegram.org/bot123:ABC/getMe"


def test_bot_username_is_empty_when_telegram_says_no():
    http = _FakeHttp(resp=_Resp(401, {"ok": False, "description": "Unauthorized"}))
    assert asyncio.run(_reg(http).bot_username(token="dead:TOK")) == ""


def test_bot_username_is_empty_on_a_transport_error():
    http = _FakeHttp(exc=RuntimeError("conn refused"))
    assert asyncio.run(_reg(http).bot_username(token="123:ABC")) == ""


def test_bot_username_without_token_never_calls_telegram():
    http = _FakeHttp()
    assert asyncio.run(_reg(http).bot_username(token="")) == ""
    assert http.calls == []


# ── added here, for branches the moving code carried untested ───────────────────────────

def test_api_base_is_a_seam_a_caller_can_point_elsewhere():
    """The override exists because a deployment may front the Bot API with a proxy, and because
    an operator script re-pointing webhooks has to be able to reach the same place this does."""
    http = _FakeHttp()
    asyncio.run(_reg(http, api_base="https://proxy.test/").register(
        token="1:A", webhook_key="k", secret="s"))
    assert http.calls[0][0] == "https://proxy.test/bot1:A/setWebhook"


def test_a_provider_5xx_never_parses_a_body():
    """``resp.json() if resp.status_code < 500 else {}`` is a real branch: a 502 from a proxy in
    front of the Bot API carries an HTML error page, and parsing it would raise INSIDE the
    ``except`` handler's blind spot — the caller would get a transport-error string instead of
    the status it actually got. Every one of the three calls takes this branch."""

    class _Exploding(_Resp):
        def json(self):
            raise ValueError("not json")

    http = _FakeHttp(resp=_Exploding(502, None))
    ok, desc = asyncio.run(_reg(http).register(token="1:A", webhook_key="k", secret="s"))
    assert ok is False and desc == "HTTP 502"
    ok, desc = asyncio.run(_reg(http).unregister(token="1:A"))
    assert ok is False and desc == "HTTP 502"
    assert asyncio.run(_reg(http).bot_username(token="1:A")) == ""


def test_the_registrar_and_the_null_answer_the_same_three_calls():
    """The null is only useful if it is substitutable: a caller that branches on which one it
    got has learned exactly what the null exists to hide."""
    for name in ("register", "unregister", "bot_username"):
        assert callable(getattr(TelegramWebhookRegistrar(webhook_base="x"), name))
        assert callable(getattr(NullTelegramRegistrar(), name))
