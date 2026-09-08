"""
cogno_gateway.telegram_provisioning — registering a Telegram bot's webhook.

The Telegram twin of :mod:`cogno_gateway.provisioning`, and it closes a hole this library
declared by omission: :class:`~cogno_gateway.telegram.TelegramChannel` VERIFIES
``X-Telegram-Bot-Api-Secret-Token`` on every delivery (fail-closed in production), and nothing
here ever told Telegram to *send* one. Verification without registration is half a lifecycle:
the application that pairs a bot from its own admin surface got a channel born with no webhook
and no secret, and the only writer in existence was an operator shell script — so "which
features work" depended on which writer ran last, and a UI reading a "registered" field nobody
set showed green over nothing.

Same division of labour as the WhatsApp half. ``TelegramChannel`` talks to a bot that is already
delivering; this brings the delivery into existence:

* :meth:`TelegramWebhookRegistrar.register` — ``setWebhook``: point the bot at the caller's
  public URL and hand Telegram the shared secret it must replay on every delivery.
* :meth:`TelegramWebhookRegistrar.unregister` — ``deleteWebhook``: the other half, so a channel
  switched off stops the provider instead of only stopping ourselves.
* :meth:`TelegramWebhookRegistrar.bot_username` — ``getMe``: the ``@handle``, which is what tells
  a human WHICH bot is connected.

It is transport, and only transport. ``webhook_key`` is an opaque string the caller chooses and
this module only ever renders into a URL; what it means (a tenant, a workspace, one bot) is the
application's decision. Errors come back as ``(ok, description)`` with Telegram's own wording —
an admin click must surface the reason, never 500 and never claim success.

:class:`NullTelegramRegistrar` is the explicit no-op for an assembly with no public URL: every
call reports WHY instead of silently reporting success, which is the failure mode a plain
``None`` produces at the call site.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The update types the webhook subscribes to — the ONE definition, for the same reason
# ``EVOLUTION_WEBHOOK_EVENTS`` is one: ``setWebhook`` REPLACES the record, so a deployment with a
# second writer in another language (the operator script that re-points webhooks after a tunnel
# rotation is the usual one) silently changes which features work whenever the two lists drift.
# Import this rather than re-typing the list.
_ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "message_reaction"]


class TelegramWebhookRegistrar:
    """Registers a bot's webhook back at the caller's own public URL, with the shared secret
    Telegram replays as ``X-Telegram-Bot-Api-Secret-Token`` — the exact header
    :meth:`cogno_gateway.telegram.TelegramChannel.verify` checks.

    ``webhook_base`` is the public base URL the CALLER serves; ``api_base`` exists so a test (or
    a proxy) can point the calls somewhere else. Pass an httpx ``AsyncClient`` or none → one is
    built per call.
    """

    def __init__(self, *, webhook_base: str, client: "Any" = None, timeout: float = 15.0,
                 api_base: str = "https://api.telegram.org") -> None:
        self._webhook_base = webhook_base.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")

    async def register(self, *, token: str, webhook_key: str, secret: str) -> "tuple[bool, str]":
        """``(ok, description)`` — ``description`` is Telegram's own wording on failure."""
        if not self._webhook_base:
            return False, "no public URL configured (COGNO_PUBLIC_URL)"
        if not token:
            return False, "bot token missing from the channel credentials"
        if not webhook_key:
            return False, "channel has no webhook_key"
        payload: dict = {"url": f"{self._webhook_base}/webhook/{webhook_key}",
                         "allowed_updates": _ALLOWED_UPDATES}
        if secret:
            payload["secret_token"] = secret
        url = f"{self._api_base}/bot{token}/setWebhook"
        try:
            resp = await self._post(url, payload)
            data = resp.json() if resp.status_code < 500 else {}
            ok = bool(data.get("ok"))
            desc = str(data.get("description", "") or f"HTTP {resp.status_code}")
            if not ok:
                # No token/secret in logs — the URL alone would leak the bot token.
                logger.warning("event=telegram_setwebhook_failed key=%s desc=%s",
                               webhook_key, desc)
            return ok, "" if ok else desc
        except Exception as exc:  # noqa: BLE001 — an admin click must see the reason
            logger.warning("event=telegram_setwebhook_error key=%s error=%s",
                           webhook_key, type(exc).__name__)
            return False, f"setWebhook request failed: {type(exc).__name__}"

    async def unregister(self, *, token: str, drop_pending: bool = True) -> "tuple[bool, str]":
        """``deleteWebhook`` — the other half of the lifecycle.

        Turning a channel off (or deleting it) only ever changed OUR side: the row goes away,
        the caller stops serving that key, and Telegram keeps delivering to a URL that now
        404s. Telegram treats that as a failed delivery, so the update sits in its queue and
        is retried — ``pending_update_count`` climbs, ``getWebhookInfo`` shows a permanent
        ``last_error`` (noise in exactly the place you look when diagnosing), and re-enabling
        the channel later replays the whole backlog at once. Tell the provider to stop.

        ``drop_pending`` decides what happens to the queue Telegram already holds. It keeps
        undelivered updates **for up to 24 hours** (Bot API: "they will not be kept longer
        than 24 hours"), so the two choices are a real trade-off and neither is free:

        * ``True`` — the backlog is discarded now. A contact who wrote during a two-minute
          maintenance window loses that message silently.
        * ``False`` — the backlog survives and is replayed when a webhook is set again. A
          channel that was off for twenty hours then answers a day-old "can you book me for
          tomorrow at 9?" as if it had just arrived.

        Nothing at this layer can tell "brief maintenance" from "off for good" — an inactive
        flag is the same signal for both — so the caller decides. Deleting a channel is the one
        unambiguous case: nobody will ever consume that queue.
        """
        if not token:
            return False, "bot token missing from the channel credentials"
        try:
            resp = await self._post(f"{self._api_base}/bot{token}/deleteWebhook",
                                    {"drop_pending_updates": drop_pending})
            data = resp.json() if resp.status_code < 500 else {}
            ok = bool(data.get("ok"))
            desc = str(data.get("description", "") or f"HTTP {resp.status_code}")
            if not ok:
                logger.warning("event=telegram_deletewebhook_failed desc=%s", desc)
            return ok, "" if ok else desc
        except Exception as exc:  # noqa: BLE001 — an admin click must see the reason
            logger.warning("event=telegram_deletewebhook_error error=%s", type(exc).__name__)
            return False, f"deleteWebhook request failed: {type(exc).__name__}"

    async def bot_username(self, *, token: str) -> str:
        """The bot's ``@handle`` via ``getMe``, or ``""``.

        A panel that lists channels has always wanted this field, and a code comment claiming it
        was "set at activation from getMe" outlived the call that would have set it — so it was
        empty for every channel the application created. It is what tells a human WHICH bot is
        connected (and what they hand to a customer to start a conversation), so an empty one
        makes the panel unreadable once more than one bot has ever been configured.

        Best-effort by design: this is a label. A failure here must never fail the activation
        that registered a working webhook.
        """
        if not token:
            return ""
        try:
            resp = await self._get(f"{self._api_base}/bot{token}/getMe")
            data = resp.json() if resp.status_code < 500 else {}
            if not data.get("ok"):
                return ""
            username = str((data.get("result") or {}).get("username", "") or "")
            return f"@{username}" if username else ""
        except Exception as exc:  # noqa: BLE001 — a label is never worth failing the call
            logger.warning("event=telegram_getme_failed error=%s", type(exc).__name__)
            return ""

    async def _get(self, url: str) -> "Any":
        if self._client is not None:
            return await self._client.get(url)
        import httpx
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(url)

    async def _post(self, url: str, payload: dict) -> "Any":
        if self._client is not None:
            return await self._client.post(url, json=payload)
        import httpx
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, json=payload)


class NullTelegramRegistrar:
    """Explicit no-op for assemblies without a public URL: every call reports the reason
    instead of silently claiming success."""

    async def register(self, *, token: str, webhook_key: str, secret: str) -> "tuple[bool, str]":
        return False, "no public URL configured (COGNO_PUBLIC_URL)"

    async def bot_username(self, *, token: str) -> str:
        return ""

    async def unregister(self, *, token: str, drop_pending: bool = True) -> "tuple[bool, str]":
        # Unlike register, this one needs no public URL — but an assembly with no registrar
        # never registered anything either, so there is nothing to take down.
        return False, "no public URL configured (COGNO_PUBLIC_URL)"


def build_telegram_registrar(env: "Optional[dict[str, str]]" = None) -> "Any":
    """A registrar when a public URL is configured, else the explicit null.

    The boot helper, so an assembly does not have to re-derive the same two-line rule: with a
    public URL, register for real; without one, hand back something that says WHY on every call
    rather than a ``None`` the call site has to remember to branch on.

    ``COGNO_PUBLIC_URL`` / ``COGNO_BASE_URL`` are read from the process environment by default,
    the same family namespace :data:`cogno_gateway.net.FAMILY_ENV` uses; ``env`` is the seam for
    an application that keeps its configuration somewhere else, and for tests.
    """
    import os
    src = env if env is not None else os.environ
    base = (src.get("COGNO_PUBLIC_URL") or src.get("COGNO_BASE_URL") or "").strip()
    if base:
        return TelegramWebhookRegistrar(webhook_base=base)
    return NullTelegramRegistrar()
