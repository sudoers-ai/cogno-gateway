"""
cogno_gateway.retry — outbound send retry for the write-safe WhatsApp channels.

Telegram has its OWN retry (``cogno_gateway.telegram._post``) and deliberately retries a
**read** timeout: on a conversational text channel a duplicate is the lesser harm against a
silently lost reply, and the loss measured there was a read-phase timeout. That decision is
left untouched.

Evolution and WhatsApp Cloud carry per-message cost / provider templates, so here the rule is
STRICT: retry ONLY where the request provably never reached the provider, so a retry cannot
duplicate a message the provider already accepted —

* a **connection** failure (``ConnectError`` / ``ConnectTimeout`` / ``PoolTimeout``) — no request
  bytes left the box;
* a **429** — the provider explicitly refused and asked to slow down (a short ``Retry-After`` is
  honoured; a long one is NOT waited out, see below);
* a **502 / 503 / 504** — a gateway/upstream error the provider did not process.

and NEVER a **read timeout**, a write error, or any other status (a 4xx the provider answered, a
500 it may have acted on): those are ambiguous or final, and repeating them risks a duplicate.

The send runs inside a turn that is holding the contact's reply open, so the whole policy is
bounded: at most :data:`MAX_ATTEMPTS` attempts with a growing backoff, and a 429 whose
``Retry-After`` exceeds :data:`MAX_RETRY_AFTER` is returned as a failure rather than waited out —
hanging the turn for a provider's minute is worse than a late send the host can re-drive.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("cogno_gateway.retry")

#: Attempts in total (the first try plus the retries), never more.
MAX_ATTEMPTS = 3
#: The growing backoff between attempts, in seconds: 0.5 → 1 → 2.
_BACKOFF = (0.5, 1.0, 2.0)
#: A 429 ``Retry-After`` longer than this (seconds) is NOT waited out — the failure is returned
#: with its reason, because the send cannot hang the turn's reply for that long.
MAX_RETRY_AFTER = 5.0
#: The statuses worth another attempt. 429 is handled on its own (``Retry-After``); the rest are
#: gateway/upstream errors the provider did not process.
_RETRIABLE_STATUS = frozenset({429, 502, 503, 504})
#: Connection-phase failures: the request bytes never left, so a retry cannot duplicate. A
#: read/write timeout or a mid-flight drop is deliberately absent — it may already have arrived.
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

#: Module-level seam so a test can make the backoff instant without touching ``asyncio``.
_sleep = asyncio.sleep


def _retry_after_seconds(response: Any) -> Optional[float]:
    """The ``Retry-After`` of a 429, in seconds, or ``None`` when it is absent/unparseable.

    Only the delta-seconds form is read — the only shape these providers send — so an HTTP-date
    ``Retry-After`` reads as absent and falls to the normal backoff rather than being mis-waited."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = (headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _retry_wait(exc: "httpx.HTTPError", attempt: int) -> Optional[float]:
    """How long to wait before the next attempt, or ``None`` to NOT retry.

    The status is read off ``exc.response`` rather than by exception class, so it works for both
    ``httpx.HTTPStatusError`` and a test double that mirrors ``.response``."""
    backoff = _BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)]
    if isinstance(exc, _CONNECT_ERRORS):
        return backoff
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        after = _retry_after_seconds(getattr(exc, "response", None))
        if after is None:
            return backoff
        return after if after <= MAX_RETRY_AFTER else None  # too long → fail, don't hang the turn
    if status in _RETRIABLE_STATUS:
        return backoff
    return None  # read timeout, write error, a 4xx/other 5xx the provider answered → never retried


async def post_with_retry(channel: str, client: "httpx.AsyncClient", url: str,
                          **kwargs: Any) -> "httpx.Response":
    """``client.post(url, **kwargs)`` + ``raise_for_status()``, retrying only the write-safe
    failures (see the module docstring). Up to :data:`MAX_ATTEMPTS` with a growing backoff.

    Each retry logs the channel, the error CLASS and the next attempt number — never the body,
    the URL or the recipient. On exhaustion, or on a non-retriable error, the last exception is
    re-raised so the caller's own ``except httpx.HTTPError`` builds its ``SendResult``."""
    attempt = 1
    while True:
        try:
            resp = await client.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            wait = _retry_wait(exc, attempt) if attempt < MAX_ATTEMPTS else None
            if wait is None:
                raise
            # WARNING: a retry is a recovered condition (LOGGING.md), and the only record that a
            # send had to be re-attempted. CLASS only — no text, URL or recipient in the line.
            logger.warning("channel=%s event=send_retry attempt=%d after=%s",
                           channel, attempt + 1, type(exc).__name__)
            await _sleep(wait)
            attempt += 1
