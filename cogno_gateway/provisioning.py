"""
cogno_gateway.provisioning — pairing a WhatsApp account with the Evolution API.

The *other* half of :mod:`cogno_gateway.evolution`. That module talks to an instance that
already exists (parse an inbound payload, send a reply); this one **brings the instance into
existence**: create it, hand back the QR to scan, poll the connection state, keep the return
address (the webhook) pointing at something alive, and tear it down.

It is transport, and only transport. A caller passes an opaque ``account`` string and the
provisioner turns it into a provider instance name and a webhook URL through two **templates it
is given** — never one it invents:

    EvolutionWhatsAppProvisioner(
        base_url=..., api_key=..., webhook_base="https://example.test",
        instance_template="myapp_{account}",
        webhook_path_template="/webhook/whatsapp_evo-{account}")

That seam is the whole reason the naming is not hardcoded here. What an ``account`` *is*
(a tenant, a workspace, a single user), how its instance is named, and which URL the caller
serves are decisions of the application on top — this library must not need to know any of
them in order to POST to Evolution. The defaults (``"{account}"`` and ``"/webhook/{account}"``)
are the identity mapping, so a caller with no convention of its own passes nothing.

``InMemoryWhatsAppProvisioner`` is a deterministic stub for dev/tests — it satisfies the same
:class:`WhatsAppProvisioner` Protocol and never touches the network.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from cogno_gateway.net import build_async_client

logger = logging.getLogger("cogno_gateway.provisioning")

# The identity mapping: an account IS its instance name, and it names its own webhook path.
# A caller with a convention passes its own template; a caller without one gets something that
# works rather than something that guesses.
DEFAULT_INSTANCE_TEMPLATE = "{account}"
DEFAULT_WEBHOOK_PATH_TEMPLATE = "/webhook/{account}"

# The events a webhook subscribes to. Kept next to the payload builder so the two cannot drift.
#
# ``PRESENCE_UPDATE`` is the one worth naming: it is what
# :meth:`cogno_gateway.evolution.EvolutionChannel.parse_presence` consumes, and Baileys is the
# only provider in this library that reports it — the Telegram Bot API has no update type for a
# USER typing and WhatsApp Cloud does not deliver presence to business webhooks at all. Without
# the subscription that method is simply never called: a reader waiting on "are they still
# typing?" degrades to a plain timer and nothing looks broken.
#
# Changing this list REQUIRES re-registering every live instance: ``webhook/set`` REPLACES the
# record, so an instance keeps its old subscription until it is rewritten.
EVOLUTION_WEBHOOK_EVENTS = ["MESSAGES_UPSERT", "CONNECTION_UPDATE", "PRESENCE_UPDATE"]


def evolution_webhook_payload(url: str, secret: str = "") -> dict:
    """The body of Evolution's ``POST /webhook/set/{instance}`` — the ONE definition.

    ``webhook/set`` REPLACES the whole record, so every writer must send every field or the
    omitted ones are reset. Deployments typically have a second writer in another language (an
    operator script that re-points webhooks after a tunnel rotation); when two copies of this
    body drifted the damage was invisible — one of them left out ``base64`` and the auth
    header, so each rotation silently un-verified the channel and broke media. Export the
    builder so the other writer calls it instead of re-encoding the body, and drift becomes
    impossible by construction rather than by review.

    The shared secret travels as a delivery **header** (``headers.apikey`` — the exact key
    :meth:`cogno_gateway.evolution.EvolutionChannel.verify` reads): Evolution persists
    ``headers`` on the webhook record and replays them on every delivery, while a top-level
    ``secret`` field matches no column and is silently dropped (verified against a deployed
    bundle, 2026-08-06). No secret → omit ``headers`` entirely; ``{"apikey": ""}`` would
    deliver an empty header instead, which is a different wire shape than "nothing".
    """
    body: dict = {"enabled": True, "url": url, "base64": True,
                  "events": list(EVOLUTION_WEBHOOK_EVENTS)}
    if secret:
        body["headers"] = {"apikey": secret}
    return {"webhook": body}


@dataclass(frozen=True)
class WhatsAppConnection:
    """The result of a connect call — a QR to scan + the instance name + a coarse status."""

    instance_name: str
    qrcode_base64: str = ""
    status: str = "pending"          # pending | connected | error


@dataclass(frozen=True)
class WhatsAppStatus:
    """The polled connection state of an account's instance."""

    state: str = "close"             # open (connected) | connecting | close | error
    instance: str = ""
    # The RETURN address, which the connection state says nothing about. An instance sits at
    # "open" while every inbound message is delivered to a dead URL: measured 2026-08-03, a
    # tunnel rotated, the instance stayed "open" for 26h, the contact's reply never reached the
    # application, and there was no error anywhere — it read exactly like "nobody answered".
    # `webhook_url` is what the PROVIDER will actually call, not what our own store believes.
    webhook_url: str = ""
    webhook_ok: bool = True          # False → it points somewhere we do not serve
    auto_healed: bool = False        # found wrong and re-pointed


@runtime_checkable
class WhatsAppProvisioner(Protocol):
    """Provision an account's WhatsApp instance via the messaging provider (Evolution).

    Async — the real adapter does HTTP; the in-memory default is for dev/tests. ``account`` is
    an opaque key chosen by the caller; this library only ever renders it through the templates
    it was given.
    """

    async def connect(self, account: str, *, force: bool = False) -> WhatsAppConnection: ...
    async def status(self, account: str) -> WhatsAppStatus: ...
    async def disconnect(self, account: str) -> bool: ...
    # The channel credentials for this account's instance — the field names of
    # :class:`cogno_gateway.types.ChannelConfig` (``token``/``base_url``/``instance``/
    # ``secret``), so a caller can persist them and later build a send-capable
    # :class:`cogno_gateway.evolution.EvolutionChannel` from the same row. {} for a stub.
    def channel_credentials(self, account: str) -> dict: ...


class InMemoryWhatsAppProvisioner:
    """A deterministic stub: connect returns a fake QR + 'pending'; a test can flip an account
    to 'open' via :meth:`mark_open`. Production injects the Evolution-backed provisioner."""

    def __init__(self, *, instance_template: str = DEFAULT_INSTANCE_TEMPLATE) -> None:
        self._state: dict[str, str] = {}          # account → state
        self._instance_template = instance_template

    def _instance(self, account: str) -> str:
        return self._instance_template.format(account=account)

    def mark_open(self, account: str) -> None:    # test hook: simulate a successful pairing
        self._state[account] = "open"

    def channel_credentials(self, account: str) -> dict:
        return {}

    async def connect(self, account: str, *, force: bool = False) -> WhatsAppConnection:
        if force:
            self._state.pop(account, None)        # recreate-from-scratch resets the pairing
        self._state[account] = "connecting"
        return WhatsAppConnection(instance_name=self._instance(account),
                                  qrcode_base64="data:image/png;base64,STUBQR", status="pending")

    async def status(self, account: str) -> WhatsAppStatus:
        return WhatsAppStatus(state=self._state.get(account, "close"),
                              instance=self._instance(account))

    async def disconnect(self, account: str) -> bool:
        return self._state.pop(account, None) is not None


class EvolutionWhatsAppProvisioner:
    """Real WhatsApp provisioning via the Evolution API. Per-account instance (named by
    ``instance_template``): connect → ``/instance/create`` (returns the pairing QR) + optional
    ``/webhook/set``; status → ``/instance/connectionState`` + a return-address probe;
    disconnect → ``/instance/delete``.

    Pass an httpx ``AsyncClient`` (or none → one is built per call). ``webhook_base`` is the
    public base URL the CALLER serves; ``webhook_path_template`` is the path on it that this
    account's inbound deliveries should land on; ``webhook_secret`` rides along as the
    ``headers.apikey`` that :meth:`cogno_gateway.evolution.EvolutionChannel.verify` checks.
    ``health_path`` is the endpoint on ``webhook_base`` used to prove that address is alive
    before writing it (see :meth:`_webhook_base_is_live`).
    """

    def __init__(self, *, base_url: str, api_key: str, webhook_base: str = "",
                 webhook_secret: str = "",
                 instance_template: str = DEFAULT_INSTANCE_TEMPLATE,
                 webhook_path_template: str = DEFAULT_WEBHOOK_PATH_TEMPLATE,
                 health_path: str = "/health",
                 client: "Any" = None, timeout: float = 45.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._webhook_base = webhook_base.rstrip("/")
        self._webhook_secret = webhook_secret
        self._instance_template = instance_template
        self._webhook_path_template = webhook_path_template
        self._health_path = health_path
        self._client = client
        self._timeout = timeout

    def _instance(self, account: str) -> str:
        return self._instance_template.format(account=account)

    async def _request(self, method: str, path: str, *, json: "Optional[dict]" = None) -> "Any":
        headers = {"apikey": self._key}
        if self._client is not None:
            return await self._client.request(method, f"{self._base}{path}", json=json,
                                              headers=headers)
        async with build_async_client(timeout=self._timeout) as client:
            return await client.request(method, f"{self._base}{path}", json=json, headers=headers)

    async def connect(self, account: str, *, force: bool = False) -> WhatsAppConnection:
        instance = self._instance(account)
        if force:
            # Recreate-from-scratch (the caller's "fix it" escape hatch): a corrupted/stuck
            # instance can keep serving a QR that never pairs — delete it and create fresh
            # (a fresh create returns the QR directly). Best-effort: 404 on delete is fine.
            await self._request("DELETE", f"/instance/delete/{instance}")
        qr = ""
        for attempt in range(3):
            resp = await self._request("POST", "/instance/create", json={
                "instanceName": instance, "integration": "WHATSAPP-BAILEYS", "qrcode": True,
                "readMessages": False, "groupsIgnore": True, "syncFullHistory": False})
            data = resp.json() if resp.status_code < 300 else {}
            qr = (data.get("qrcode", {}) or {}).get("base64", "") or data.get("base64", "")
            if qr or resp.status_code < 300:
                break
            # Right after a force-delete, Evolution briefly holds the name ("already in use"
            # while the old instance tears down) and the immediate re-create fails — live
            # 2026-07-20: force deleted the instance and left NOTHING behind. Give the
            # teardown a beat and retry; a plain existing-instance 403 exits via the
            # GET /instance/connect fallback below on the last attempt.
            if attempt < 2:
                await asyncio.sleep(2)
        if not qr:
            # The instance already exists (create → 403 "already in use" — every reconnect after
            # the first pairing, or a stale/closed instance after a reboot): a fresh QR comes from
            # GET /instance/connect/{name} instead. The base64 image may take a beat to be
            # generated after the connection cycle kicks, so poll briefly. Live 2026-07-20: a
            # dashboard's "generate QR" showed "unavailable" forever on an existing instance.
            for _ in range(4):
                r2 = await self._request("GET", f"/instance/connect/{instance}")
                d2 = r2.json() if r2.status_code < 300 else {}
                qr = d2.get("base64", "") or (d2.get("qrcode", {}) or {}).get("base64", "")
                if qr:
                    break
                await asyncio.sleep(2)
        if self._webhook_base:                                # route inbound back to the caller
            await self._request("POST", f"/webhook/set/{instance}",
                                json=self._webhook_payload(self._webhook_url(account)))
        return WhatsAppConnection(instance_name=instance, qrcode_base64=qr,
                                  status="connected" if qr else "pending")

    def _webhook_payload(self, url: str) -> dict:
        """This instance's ``webhook/set`` body — see :func:`evolution_webhook_payload`, which
        an operator script calls too so there is only ever one definition."""
        return evolution_webhook_payload(url, self._webhook_secret)

    def channel_credentials(self, account: str) -> dict:
        return {"token": self._key, "base_url": self._base,
                "instance": self._instance(account), "secret": self._webhook_secret}

    def _webhook_url(self, account: str) -> str:
        return f"{self._webhook_base}{self._webhook_path_template.format(account=account)}"

    async def _webhook_base_is_live(self) -> bool:
        """Does the address we are about to advertise actually answer?

        Pointing the provider at an unreachable URL is the exact failure the heal exists to
        undo, and it is silent. From a dashboard visit it hardly matters — whoever opened the
        page has the system up. From a scheduled check it matters a lot: with the tunnel down
        at 3am, an unguarded heal would rewrite EVERY account's webhook to a dead address,
        turning a transient outage into a persistent one. Fail-CLOSED: unknown → do not write.
        """
        try:
            if self._client is not None:
                resp = await self._client.request(
                    "GET", f"{self._webhook_base}{self._health_path}", json=None, headers={})
            else:
                async with build_async_client(timeout=10.0) as client:
                    resp = await client.request(
                        "GET", f"{self._webhook_base}{self._health_path}")
            return int(getattr(resp, "status_code", 500)) < 300
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=webhook_base_unreachable base=%s error=%s",
                           self._webhook_base, type(exc).__name__)
            return False

    async def status(self, account: str) -> WhatsAppStatus:
        instance = self._instance(account)
        resp = await self._request("GET", f"/instance/connectionState/{instance}")
        data = resp.json() if resp.status_code < 300 else {}
        state = ((data.get("instance", {}) or {}).get("state")
                 or data.get("state") or "close")
        # WhatsAppStatus is frozen — build the final value, never mutate a draft.
        if not self._webhook_base:
            return WhatsAppStatus(state=state, instance=instance)
        # Ask the PROVIDER where it will deliver, and compare with where we listen. Reading our
        # own store instead would be a mirror: it held the right URL all along while Evolution
        # held a stale one, and the dashboard showed green while the contact's replies went
        # nowhere.
        expected = self._webhook_url(account)
        try:
            found = await self._request("GET", f"/webhook/find/{instance}")
            record = (found.json() or {}) if found.status_code < 300 else {}
            actual = record.get("url", "")
        except Exception as exc:  # noqa: BLE001 — a probe must never break the status call
            logger.warning("event=webhook_probe_failed instance=%s error=%s",
                           instance, type(exc).__name__)
            return WhatsAppStatus(state=state, instance=instance)

        # Staleness is URL *or* auth header: a re-register that kept the URL but dropped
        # ``headers.apikey`` (a pre-fix operator script did exactly that) leaves a webhook whose
        # every delivery the channel rejects — same outage, invisible to a URL-only check.
        # Never log the header values themselves, only which check failed.
        header_ok = (not self._webhook_secret
                     or (record.get("headers") or {}).get("apikey", "") == self._webhook_secret)
        if actual == expected and header_ok:
            return WhatsAppStatus(state=state, instance=instance,
                                  webhook_url=actual, webhook_ok=True)

        logger.warning("event=webhook_stale instance=%s reason=%s actual=%s expected=%s",
                       instance, "url" if actual != expected else "auth_header",
                       actual, expected)
        stale = WhatsAppStatus(state=state, instance=instance,
                               webhook_url=actual, webhook_ok=False)
        if not await self._webhook_base_is_live():
            logger.warning("event=webhook_heal_skipped instance=%s reason=base_unreachable",
                           instance)
            return stale
        try:
            await self._request("POST", f"/webhook/set/{instance}",
                                json=self._webhook_payload(expected))
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=webhook_heal_failed instance=%s error=%s",
                           instance, type(exc).__name__)
            return stale
        return WhatsAppStatus(state=state, instance=instance, webhook_url=expected,
                              webhook_ok=True, auto_healed=True)

    async def disconnect(self, account: str) -> bool:
        instance = self._instance(account)
        resp = await self._request("DELETE", f"/instance/delete/{instance}")
        return resp.status_code < 300
