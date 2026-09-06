"""Unit tests for WhatsApp/Evolution provisioning — connect (QR), status, disconnect.

Two things are under test here and they are not the same thing:

* the **transport** — what goes on the wire to Evolution, in which order, and what comes back
  as a :class:`WhatsAppConnection` / :class:`WhatsAppStatus`;
* the **naming seam** — this library renders an opaque ``account`` into an instance name and a
  webhook URL through templates it is GIVEN. That is the property that lets an application keep
  its own conventions (what an account is, how instances are named, which URL it serves)
  without this library needing to know any of them, and the ``_TEMPLATE SEAM`` section below
  is what fails if it is ever hardcoded back.
"""

from __future__ import annotations

import asyncio

import pytest

from cogno_gateway import (
    EVOLUTION_WEBHOOK_EVENTS,
    ChannelConfig,
    EvolutionChannel,
    EvolutionWhatsAppProvisioner,
    InMemoryWhatsAppProvisioner,
    WhatsAppProvisioner,
    evolution_webhook_payload,
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Collapse the retry back-off so the suite stays sub-second.

    The waits are real (2s each) and there are up to seven of them on the existing-instance
    path — twelve seconds for one test. The tests below assert on the SEQUENCE of calls, never
    on elapsed time, so replacing the delay with a zero-length yield changes nothing they
    measure. ``test_the_backoff_is_actually_awaited`` counts the sleeps, so "no back-off at
    all" is still a failure rather than a speed-up.
    """
    real = asyncio.sleep
    slept: list = []

    async def _fast(delay, *a, **k):
        slept.append(delay)
        return await real(0)

    monkeypatch.setattr(asyncio, "sleep", _fast)
    return slept


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._p = payload or {}

    def json(self):
        return self._p


class _FakeHttp:
    """A fake httpx AsyncClient: canned responses by (method, url suffix), calls recorded."""

    def __init__(self, routes=None):
        self.routes = routes or {}          # (method, path_suffix) → _Resp
        self.calls: list = []

    async def request(self, method, url, *, json=None, headers=None):
        self.calls.append((method, url, json, headers))
        for (m, suffix), resp in self.routes.items():
            if method == m and url.endswith(suffix):
                return resp
        return _Resp(404, {})


# The host-shaped conventions used throughout: an application names its instances and its
# webhook path however it likes, and passes those two strings in.
INSTANCE_T = "app_{account}"
WEBHOOK_T = "/webhook/whatsapp_evo-{account}"


def _evo(routes=None, **kw):
    http = _FakeHttp(routes)
    kw.setdefault("instance_template", INSTANCE_T)
    kw.setdefault("webhook_path_template", WEBHOOK_T)
    return http, EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                              client=http, **kw)


def _webhook_set_bodies(http):
    return [c[2] for c in http.calls if "/webhook/set/" in c[1]]


# ── the payload builder: one definition, and `webhook/set` REPLACES the record ─────────────

def test_payload_carries_enabled_base64_and_the_events():
    body = evolution_webhook_payload("https://x/webhook/k")["webhook"]
    assert body["enabled"] is True and body["url"] == "https://x/webhook/k"
    # base64 is what makes inbound MEDIA readable; an omitting writer broke media silently.
    assert body["base64"] is True
    assert body["events"] == list(EVOLUTION_WEBHOOK_EVENTS)


def test_the_subscribed_events_are_exactly_the_ones_this_library_can_PARSE():
    """Lockstep between what we ask Evolution to send and what we do with it.

    Two directions, and the second is the one that bites. Subscribing to an event nobody reads
    is only noise; NOT subscribing to one the code depends on is a feature that silently does
    nothing. ``PRESENCE_UPDATE`` is the case in point: ``EvolutionChannel.parse_presence``
    exists solely to consume it, and without the subscription a reader waiting on "are they
    still typing?" degrades to a plain timer — the replies still arrive, so nothing looks
    broken. The only thing that would tell anyone is this assertion.
    """
    assert set(EVOLUTION_WEBHOOK_EVENTS) == {
        "MESSAGES_UPSERT",      # → EvolutionChannel.parse_inbound
        "CONNECTION_UPDATE",    # → instance status (connected/disconnected)
        "PRESENCE_UPDATE",      # → EvolutionChannel.parse_presence
    }, "an event was added or dropped without deciding who consumes it"


def test_secret_rides_as_headers_apikey_and_the_dead_field_stays_dead():
    body = evolution_webhook_payload("https://x/webhook/k", "s3cret")["webhook"]
    assert body["headers"] == {"apikey": "s3cret"}
    # Evolution has no column for a top-level `secret`: it is dropped without an error, and the
    # channel ran open for a month while the code LOOKED like it was sending one.
    assert "secret" not in body


def test_no_secret_omits_headers_entirely():
    """``{"apikey": ""}`` would make Evolution send an empty header on every delivery — a
    different wire shape than "nothing", and one a stricter verify could trip over."""
    assert "headers" not in evolution_webhook_payload("https://x/webhook/k", "")["webhook"]


# ── the in-memory stub ─────────────────────────────────────────────────────────────────────

async def test_in_memory_stub_pairs_and_tears_down():
    prov = InMemoryWhatsAppProvisioner(instance_template=INSTANCE_T)
    conn = await prov.connect("acct")
    assert conn.instance_name == "app_acct" and conn.status == "pending"
    assert conn.qrcode_base64.startswith("data:image/png")
    assert (await prov.status("acct")).state == "connecting"
    prov.mark_open("acct")
    assert (await prov.status("acct")).state == "open"
    assert await prov.disconnect("acct") is True
    assert (await prov.status("acct")).state == "close"
    assert await prov.disconnect("acct") is False       # nothing left to tear down


async def test_in_memory_force_resets_the_pairing_and_reports_no_credentials():
    prov = InMemoryWhatsAppProvisioner()
    prov.mark_open("acct")
    await prov.connect("acct", force=True)
    assert (await prov.status("acct")).state == "connecting"
    assert prov.channel_credentials("acct") == {}       # a stub has nothing to persist


def test_both_provisioners_satisfy_the_protocol():
    """``WhatsAppProvisioner`` is the seam an application depends on; a stub that drifts out of
    it turns into an AttributeError at the call site instead of a failure here."""
    assert isinstance(InMemoryWhatsAppProvisioner(), WhatsAppProvisioner)
    assert isinstance(EvolutionWhatsAppProvisioner(base_url="x", api_key="k"),
                      WhatsAppProvisioner)


# ── connect ────────────────────────────────────────────────────────────────────────────────

async def test_connect_returns_the_qr_and_registers_the_webhook():
    http, prov = _evo({
        ("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QRDATA"}}),
        ("POST", "/webhook/set/app_acct"): _Resp(200, {}),
    }, webhook_base="https://host.test")
    conn = await prov.connect("acct")
    assert conn.instance_name == "app_acct" and conn.qrcode_base64 == "QRDATA"
    assert conn.status == "connected"
    assert http.calls[0][3]["apikey"] == "k"            # the provider api key on every call
    assert _webhook_set_bodies(http)[0]["webhook"]["url"] == \
        "https://host.test/webhook/whatsapp_evo-acct"


async def test_connect_without_a_webhook_base_registers_nothing():
    """No public address to advertise → do not write one. (An empty URL would be worse than
    none: Evolution would accept the record and deliver nowhere.)"""
    http, prov = _evo({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}})})
    assert (await prov.connect("acct")).qrcode_base64 == "QR"
    assert not _webhook_set_bodies(http)


async def test_connect_reads_a_top_level_base64_too():
    """Builds differ on where the QR sits — ``qrcode.base64`` or a bare ``base64``."""
    _, prov = _evo({("POST", "/instance/create"): _Resp(201, {"base64": "FLATQR"})})
    assert (await prov.connect("acct")).qrcode_base64 == "FLATQR"


async def test_connect_falls_back_when_the_instance_already_exists():
    """Live 2026-07-20: on an EXISTING instance ``/instance/create`` → 403 "already in use" and
    the QR showed "unavailable" forever. The fresh QR comes from ``GET /instance/connect``."""
    http, prov = _evo({
        ("POST", "/instance/create"): _Resp(
            403, {"status": 403, "error": "Forbidden",
                  "response": {"message": ['This name "app_acct" is already in use.']}}),
        ("GET", "/instance/connect/app_acct"): _Resp(
            200, {"pairingCode": None, "code": "2@abc",
                  "base64": "data:image/png;base64,QR2", "count": 1}),
        ("POST", "/webhook/set/app_acct"): _Resp(200, {}),
    }, webhook_base="https://host.test")
    conn = await prov.connect("acct")
    assert conn.qrcode_base64 == "data:image/png;base64,QR2" and conn.status == "connected"


async def test_connect_polls_until_the_qr_image_fills():
    """The base64 image lags the connection cycle: the first GET answers code-only."""

    class _Seq(_FakeHttp):
        n = 0

        async def request(self, method, url, *, json=None, headers=None):
            self.calls.append((method, url, json, headers))
            if url.endswith("/instance/create"):
                return _Resp(403, {})
            if "/instance/connect/" in url:
                self.n += 1
                return _Resp(200, {"code": "2@x", "base64": "" if self.n == 1 else "QR-LATE"})
            return _Resp(200, {})

    http = _Seq()
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k", client=http,
                                        instance_template=INSTANCE_T)
    conn = await prov.connect("acct")
    assert conn.qrcode_base64 == "QR-LATE" and conn.status == "connected"


async def test_connect_gives_up_pending_rather_than_inventing_a_qr():
    """Nothing anywhere returns an image → ``pending``, and the caller can re-poll status. A
    'connected' with an empty QR would be a lie the dashboard cannot recover from."""
    http, prov = _evo({("POST", "/instance/create"): _Resp(403, {})})
    conn = await prov.connect("acct")
    assert conn.qrcode_base64 == "" and conn.status == "pending"


async def test_force_deletes_before_it_creates():
    http, prov = _evo({
        ("DELETE", "/instance/delete/app_acct"): _Resp(200, {}),
        ("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "FRESHQR"}}),
    })
    conn = await prov.connect("acct", force=True)
    assert conn.qrcode_base64 == "FRESHQR"
    ordered = [(m, u) for m, u, *_ in http.calls if "/instance/" in u]
    assert ordered[0][0] == "DELETE" and ordered[1][0] == "POST"


async def test_force_retries_the_create_after_the_name_lock_race():
    """Live 2026-07-20: right after a force-delete Evolution still holds the name, the
    immediate re-create 403s, and the account ended with NO instance at all."""

    class _Race(_FakeHttp):
        creates = 0

        async def request(self, method, url, *, json=None, headers=None):
            self.calls.append((method, url, json, headers))
            if method == "DELETE":
                return _Resp(200, {})
            if url.endswith("/instance/create"):
                self.creates += 1
                if self.creates == 1:      # name still locked by the teardown
                    return _Resp(403, {"response": {"message": ["already in use"]}})
                return _Resp(201, {"qrcode": {"base64": "FRESH-AFTER-RETRY"}})
            return _Resp(200, {})

    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        client=_Race(), instance_template=INSTANCE_T)
    conn = await prov.connect("acct", force=True)
    assert conn.qrcode_base64 == "FRESH-AFTER-RETRY" and conn.status == "connected"


async def test_the_backoff_is_actually_awaited(_no_backoff):
    """The fixture above collapses the wait; this pins that a wait HAPPENS. Without it the
    retry re-fires inside the same millisecond and the teardown has not moved on."""
    http, prov = _evo({("POST", "/instance/create"): _Resp(403, {})})
    await prov.connect("acct")
    assert _no_backoff, "the retry loop never waited — the name lock has no time to clear"
    assert all(d > 0 for d in _no_backoff)


# ── status: the connection state, and the RETURN address it says nothing about ─────────────

_OPEN = {("GET", "/instance/connectionState/app_acct"):
         _Resp(200, {"instance": {"state": "open"}})}
_STALE = {("GET", "/webhook/find/app_acct"):
          _Resp(200, {"url": "https://DEAD.tunnel/webhook/whatsapp_evo-acct"})}
_LIVE_BASE = {("GET", "https://host.test/health"): _Resp(200, {})}


async def test_status_reads_a_flat_state_too():
    _, prov = _evo({("GET", "/instance/connectionState/app_acct"):
                    _Resp(200, {"state": "connecting"})})
    assert (await prov.status("acct")).state == "connecting"


async def test_status_defaults_to_close_when_the_provider_errors():
    _, prov = _evo({})                                   # everything 404s
    assert (await prov.status("acct")).state == "close"


async def test_status_without_a_webhook_base_does_not_probe():
    http, prov = _evo(_OPEN)
    st = await prov.status("acct")
    assert st.state == "open" and st.webhook_url == "" and st.webhook_ok is True
    assert not any("/webhook/find/" in c[1] for c in http.calls)


async def test_status_heals_a_stale_return_address():
    """An instance sits at state="open" while every inbound message goes to a dead URL.
    Measured 2026-08-03: a tunnel rotated, the instance stayed "open" for 26h, the contact's
    reply never arrived, and there was no error anywhere — it read as "nobody answered". The
    address comes from the PROVIDER: our own store held the correct URL the whole time."""
    http, prov = _evo({**_OPEN, **_STALE, **_LIVE_BASE,
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_base="https://host.test")
    st = await prov.status("acct")
    assert st.state == "open"                            # the connection was never the problem
    assert st.auto_healed is True and st.webhook_ok is True
    assert st.webhook_url == "https://host.test/webhook/whatsapp_evo-acct"
    assert _webhook_set_bodies(http)


async def test_status_leaves_a_correct_return_address_alone():
    http, prov = _evo({**_OPEN, ("GET", "/webhook/find/app_acct"):
                       _Resp(200, {"url": "https://host.test/webhook/whatsapp_evo-acct"})},
                      webhook_base="https://host.test")
    st = await prov.status("acct")
    assert st.webhook_ok is True and st.auto_healed is False
    assert not _webhook_set_bodies(http)                 # no needless write


async def test_heal_is_skipped_when_our_own_public_url_is_dead():
    """Fail-CLOSED. From a dashboard visit an unguarded heal hardly matters — whoever opened the
    page has the system up. From a scheduled check it matters a lot: with the tunnel down at 3am
    it would rewrite EVERY account's webhook to a dead address, turning a transient outage into
    a persistent one. Report the stale address; do not write a worse one."""
    http, prov = _evo({**_OPEN, **_STALE}, webhook_base="https://host.test")
    st = await prov.status("acct")
    assert st.webhook_ok is False and st.auto_healed is False
    assert st.webhook_url == "https://DEAD.tunnel/webhook/whatsapp_evo-acct"
    assert not _webhook_set_bodies(http)


async def test_a_raising_liveness_probe_is_read_as_dead_not_as_alive():
    class _Boom(_FakeHttp):
        async def request(self, method, url, *, json=None, headers=None):
            if "/health" in url:
                raise RuntimeError("tunnel down")
            return await super().request(method, url, json=json, headers=headers)

    http = _Boom({**_OPEN, **_STALE, ("POST", "/webhook/set/app_acct"): _Resp(200, {})})
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_base="https://host.test", client=http,
                                        instance_template=INSTANCE_T,
                                        webhook_path_template=WEBHOOK_T)
    st = await prov.status("acct")
    assert st.auto_healed is False and not _webhook_set_bodies(http)


async def test_a_failing_probe_never_breaks_the_status_call():
    """The probe is a nicety; the connection state is what the caller came for."""

    class _Boom(_FakeHttp):
        async def request(self, method, url, *, json=None, headers=None):
            if "/webhook/find/" in url:
                raise RuntimeError("evolution down")
            return await super().request(method, url, json=json, headers=headers)

    http = _Boom(_OPEN)
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_base="https://host.test", client=http,
                                        instance_template=INSTANCE_T)
    st = await prov.status("acct")
    assert st.state == "open" and st.auto_healed is False and st.webhook_ok is True


async def test_a_failing_heal_write_reports_stale_instead_of_claiming_success():
    class _Boom(_FakeHttp):
        async def request(self, method, url, *, json=None, headers=None):
            if "/webhook/set/" in url:
                raise RuntimeError("write refused")
            return await super().request(method, url, json=json, headers=headers)

    http = _Boom({**_OPEN, **_STALE, **_LIVE_BASE})
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_base="https://host.test", client=http,
                                        instance_template=INSTANCE_T,
                                        webhook_path_template=WEBHOOK_T)
    st = await prov.status("acct")
    assert st.webhook_ok is False and st.auto_healed is False


async def test_status_flags_a_dropped_auth_header_as_stale_and_heals_it():
    """URL right, header gone (what a pre-fix operator script left behind): a URL-only check
    reports webhook_ok=True while the channel rejects every delivery. The header is part of
    the return address."""
    http, prov = _evo({**_OPEN, **_LIVE_BASE,
                       ("GET", "/webhook/find/app_acct"): _Resp(200, {
                           "url": "https://host.test/webhook/whatsapp_evo-acct",
                           "headers": {"apikey": ""}}),
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_base="https://host.test", webhook_secret="s3cret")
    st = await prov.status("acct")
    assert st.auto_healed is True and st.webhook_ok is True
    assert _webhook_set_bodies(http)[0]["webhook"]["headers"] == {"apikey": "s3cret"}


async def test_status_accepts_a_matching_auth_header_without_rewriting():
    http, prov = _evo({**_OPEN,
                       ("GET", "/webhook/find/app_acct"): _Resp(200, {
                           "url": "https://host.test/webhook/whatsapp_evo-acct",
                           "headers": {"apikey": "s3cret"}})},
                      webhook_base="https://host.test", webhook_secret="s3cret")
    st = await prov.status("acct")
    assert st.webhook_ok is True and st.auto_healed is False
    assert not _webhook_set_bodies(http)


async def test_disconnect_reports_the_provider_verdict():
    http, prov = _evo({("DELETE", "/instance/delete/app_acct"): _Resp(200, {})})
    assert await prov.disconnect("acct") is True
    assert http.calls[0][1] == "https://evo.test/instance/delete/app_acct"
    _, gone = _evo({})                                   # 404 → nothing to disconnect
    assert await gone.disconnect("acct") is False


# ── the secret: the wire copy and the stored copy must be the SAME value ───────────────────

async def test_connect_sends_the_secret_as_headers_apikey():
    http, prov = _evo({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}}),
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_base="https://host.test", webhook_secret="s3cret")
    await prov.connect("acct")
    bodies = _webhook_set_bodies(http)
    assert bodies, "connect never registered the webhook"
    assert bodies[0]["webhook"]["headers"] == {"apikey": "s3cret"}
    assert "secret" not in bodies[0]["webhook"]


async def test_credentials_match_what_went_on_the_wire_and_build_a_working_channel():
    """The stored copy (what ``verify`` reads) and the wire copy (what Evolution replays) must
    be the SAME value, or the channel goes mute on the first verified delivery. Round-trip it
    through the real :class:`EvolutionChannel` rather than comparing two strings: if either
    side renames its key ("apikey" ↔ anything) this breaks here instead of live."""
    http, prov = _evo({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}}),
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_base="https://host.test", webhook_secret="s3cret")
    await prov.connect("acct")
    wire = _webhook_set_bodies(http)[0]["webhook"]["headers"]
    creds = prov.channel_credentials("acct")
    assert creds == {"token": "k", "base_url": "https://evo.test",
                     "instance": "app_acct", "secret": "s3cret"}
    ch = EvolutionChannel(ChannelConfig(token=creds["token"], base_url=creds["base_url"],
                                        instance=creds["instance"], secret=creds["secret"]))
    # a caller lowercases header names before verify; "apikey" is already lowercase — assert
    # that too, or the round-trip claim is weaker than the wire.
    assert all(k == k.lower() for k in wire)
    assert ch.verify(headers=wire, body=b"") is True


def test_the_provisioner_body_is_the_shared_function_not_a_second_copy():
    """The provisioner and any operator script must render the SAME body — when two copies
    drifted, one dropped base64 + the auth header and every tunnel rotation silently
    un-verified the channel. One definition; this pins the delegation."""
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_secret="s3cret")
    url = "https://host.test/webhook/whatsapp_evo-acct"
    assert prov._webhook_payload(url) == evolution_webhook_payload(url, "s3cret")
    bare = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k")
    assert bare._webhook_payload(url) == evolution_webhook_payload(url, "")


# ── THE TEMPLATE SEAM: the naming belongs to the caller, not to this library ───────────────

async def test_the_instance_template_names_the_instance_on_EVERY_call():
    """One template, four endpoints. A hardcoded name anywhere would still pass a test that
    only checked ``connect``, and then delete the wrong instance."""
    http, prov = _evo({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}}),
                       ("POST", "/webhook/set/wa-acct-prod"): _Resp(200, {}),
                       ("GET", "/instance/connectionState/wa-acct-prod"):
                           _Resp(200, {"instance": {"state": "open"}}),
                       ("GET", "/webhook/find/wa-acct-prod"):
                           _Resp(200, {"url": "https://host.test/hook/acct"}),
                       ("DELETE", "/instance/delete/wa-acct-prod"): _Resp(200, {}),
                       ("GET", "https://host.test/health"): _Resp(200, {})},
                      instance_template="wa-{account}-prod",
                      webhook_path_template="/hook/{account}",
                      webhook_base="https://host.test")
    assert (await prov.connect("acct")).instance_name == "wa-acct-prod"
    assert (await prov.status("acct")).instance == "wa-acct-prod"
    assert await prov.disconnect("acct") is True
    assert prov.channel_credentials("acct")["instance"] == "wa-acct-prod"
    # …and nothing on the wire ever named it anything else. `create` carries the name in the
    # BODY and every other endpoint in the PATH, so both are checked — a hardcode in one of
    # them survives a test that only reads the other.
    named = 0
    for _m, url, body, _h in http.calls:
        if url.endswith("/instance/create"):
            assert body["instanceName"] == "wa-acct-prod"
            named += 1
        elif "/instance/" in url or "/webhook/set/" in url or "/webhook/find/" in url:
            assert url.endswith("/wa-acct-prod"), url
            named += 1
    assert named == 5, ("expected create + webhook-set + connectionState + webhook-find + "
                        f"delete to name it, saw {named}")


async def test_the_webhook_path_template_builds_the_registered_url():
    http, prov = _evo({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}}),
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_path_template="/inbound/evo/{account}",
                      webhook_base="https://host.test/")   # trailing slash must not double up
    await prov.connect("acct")
    assert _webhook_set_bodies(http)[0]["webhook"]["url"] == \
        "https://host.test/inbound/evo/acct"


async def test_the_defaults_are_the_identity_mapping_not_a_guess():
    """A caller with no convention of its own gets something that works: the account IS the
    instance name, and it names its own webhook path."""
    http = _FakeHttp({("POST", "/instance/create"): _Resp(201, {"qrcode": {"base64": "QR"}}),
                      ("POST", "/webhook/set/acct"): _Resp(200, {})})
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_base="https://host.test", client=http)
    assert (await prov.connect("acct")).instance_name == "acct"
    assert _webhook_set_bodies(http)[0]["webhook"]["url"] == "https://host.test/webhook/acct"
    assert InMemoryWhatsAppProvisioner()._instance("acct") == "acct"


async def test_the_health_path_is_the_callers_too():
    """The liveness probe hits an endpoint on the CALLER's base URL. A library that assumed
    ``/health`` would read every application that serves it elsewhere as permanently dead — and
    fail-closed then means the heal never runs at all."""
    http, prov = _evo({**_OPEN, **_STALE,
                       ("GET", "https://host.test/_alive"): _Resp(200, {}),
                       ("POST", "/webhook/set/app_acct"): _Resp(200, {})},
                      webhook_base="https://host.test", health_path="/_alive")
    assert (await prov.status("acct")).auto_healed is True
    assert any(u == "https://host.test/_alive" for _, u, *_ in http.calls)


# ── the no-client path: the provisioner builds its own httpx client ────────────────────────

async def test_without_an_injected_client_it_opens_its_own(monkeypatch):
    """The default construction (no ``client=``) is what production uses; the injected client
    is a test affordance. Cover the branch that would otherwise only ever run live."""
    import httpx

    seen: list = []

    class _Client:
        def __init__(self, *a, **k):
            seen.append(k.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kw):
            seen.append((method, url))
            return _Resp(200, {"qrcode": {"base64": "QR"}})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    prov = EvolutionWhatsAppProvisioner(base_url="https://evo.test", api_key="k",
                                        webhook_base="https://host.test", timeout=7.0)
    assert (await prov.connect("acct")).qrcode_base64 == "QR"
    assert 7.0 in seen
    assert await prov._webhook_base_is_live() is True
