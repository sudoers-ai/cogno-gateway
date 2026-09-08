"""The address-family fallback: what happens when one family answers and the other does not.

**The defect these tests are written against.** A reply did not leave the box because the
connection to the provider bound to an IPv6 address that completed the TCP handshake and then
never finished the TLS one. The stack below us does less than it looks like it does: anyio races
the *TCP* connect across families (RFC 6555), but ``httpcore`` then runs ``start_tls`` on the one
stream that won that race, with no address left to fall back to — and the address that answers
the TCP handshake instantly is exactly the one that wins the race. It was contained by hand with
a line in ``/etc/hosts``, which is not code: nothing in a test can see it, it does not travel with
a deploy, and it does not survive the provider changing its addresses.

**No network here, in either direction.** Both seams are replaced: the resolver never looks a name
up, and the opener never opens a socket. The addresses below come from the documentation ranges
reserved for exactly this (RFC 3849 ``2001:db8::/32`` and RFC 5737 ``192.0.2.0/24``) — no real
host, no real infrastructure, nothing anybody operates.

**How the time is measured, stated rather than implied: a VIRTUAL clock, plus the attempt count.**
``FakeNetwork.elapsed`` is not a wall clock — it is the sum of the budgets the failed attempts were
handed, charged by the fake at the moment it decides an address is dead. Wall-clock assertions are
the wrong instrument on a loaded box (they measure the box), and an attempt count alone cannot see
the second mandatory mutation, which does not change how many attempts happen but how long each one
is allowed to take. So both are asserted: ``len(net.attempts)`` says how many addresses were tried,
``net.elapsed`` says what the failures cost.
"""

import logging
import socket

import httpcore
import httpx
import pytest

from cogno_gateway import ChannelConfig, OutboundMessage, TelegramChannel
from cogno_gateway import net
from cogno_gateway.chunker import split_message
from cogno_gateway.net import (
    AUTO,
    DEFAULT_CONNECT_TIMEOUT,
    FAMILIES_EXHAUSTED,
    IPV4,
    IPV6,
    Candidate,
    FamilyFallbackBackend,
    FamilyFallbackTransport,
    build_async_client,
    connect_timeout,
    ip_family,
    order_candidates,
)

# Documentation-only addresses (RFC 3849 / RFC 5737). They are not routable and belong to nobody.
V6 = "2001:db8::1"
V4 = "192.0.2.1"
# The platform's own constants and not the numbers they happen to be on Linux: the fake resolver
# stands in for ``getaddrinfo``, so it must hand back what ``getaddrinfo`` hands back — 10 and 2
# here, 30 and 2 on a BSD, and a test that hard-codes one of those is testing this machine.
AF_INET6 = int(socket.AF_INET6)
AF_INET = int(socket.AF_INET)
BOTH = ((AF_INET6, V6), (AF_INET, V4))

CFG = ChannelConfig(token="BOT123", secret="s")


# ── the fake network ─────────────────────────────────────────────────────────
def http_ok(body: bytes) -> bytes:
    """One complete HTTP/1.1 response, so the REAL httpcore parser runs over it."""
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)


TELEGRAM_OK = b'{"ok":true,"result":{"message_id":1}}'
EVOLUTION_OK = b'{"key":{"id":"m1"}}'


class _ScriptedStream(httpcore.AsyncMockStream):
    """A connected stream that can refuse the TLS handshake.

    Subclasses httpcore's own mock so the bytes above go through the real HTTP/1.1 machinery; the
    only thing overridden is ``start_tls``, which is the half of the bind the production stack
    does not fall back on and therefore the half these tests exist for."""

    def __init__(self, network: "FakeNetwork", address: str, buffer: list) -> None:
        super().__init__(list(buffer))
        self._network = network
        self._address = address

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._network.handshakes.append((self._address, server_hostname))
        if self._address in self._network.tls_dead:
            # A black-holed handshake spends the whole budget it was given and then gives up.
            self._network.elapsed += timeout or 0.0
            raise httpcore.ConnectTimeout("tls handshake never completed")
        return self


class FakeNetwork:
    """A resolver + an opener, with a verdict per address and a virtual clock.

    ``tcp_dead`` never accepts a connection; ``tls_dead`` accepts one and then hangs in the
    handshake — the measured shape, and the one a connect-only fallback would not survive."""

    def __init__(self, *, addresses=BOTH, tcp_dead=(), tls_dead=(), body=TELEGRAM_OK,
                 responses=32) -> None:
        self.addresses = list(addresses)
        self.tcp_dead = set(tcp_dead)
        self.tls_dead = set(tls_dead)
        self._buffer = [http_ok(body)] * responses
        self.resolutions = 0
        self.attempts: list[tuple[str, float | None]] = []
        self.handshakes: list[tuple[str, str | None]] = []
        self.elapsed = 0.0

    async def resolve(self, host, port):
        self.resolutions += 1
        return list(self.addresses)

    async def open(self, address, port, timeout=None, local_address=None, socket_options=None):
        self.attempts.append((address, timeout))
        if address in self.tcp_dead:
            self.elapsed += timeout or 0.0
            raise httpcore.ConnectTimeout("no route to that address")
        return _ScriptedStream(self, address, self._buffer)

    @property
    def tried(self) -> list[str]:
        return [address for address, _budget in self.attempts]


@pytest.fixture
def fake_network(monkeypatch):
    """Install a :class:`FakeNetwork` on the two module-level seams the adapters reach.

    Patched by NAME on ``cogno_gateway.net`` and not passed to a constructor: the adapters build
    their client with no arguments, so a seam only reachable through a constructor is a seam no
    test of the real send path can use."""

    def install(**kwargs) -> FakeNetwork:
        network = FakeNetwork(**kwargs)
        monkeypatch.setattr(net, "resolve_addresses", network.resolve)
        monkeypatch.setattr(net, "_open_tcp", network.open)
        return network

    return install


class _InstantSleep:
    """Stands in for ``telegram``'s ``asyncio`` so the retry's backoff is free and recorded."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def no_backoff(monkeypatch):
    from cogno_gateway import telegram as tg
    clock = _InstantSleep()
    monkeypatch.setattr(tg, "asyncio", clock)
    return clock


# ── TWIN 1 — the measured shape: IPv6 answers TCP, then hangs in the handshake ───
async def test_a_dead_ipv6_handshake_still_delivers_over_ipv4(fake_network, caplog):
    """The reply goes out, over the family that works, for the price of one connect budget.

    This is the twin the whole module exists for, and the one that separates this fix from the
    fix that reads the same: the IPv6 address here is not unreachable, it is *reachable and
    useless* — it completes the TCP handshake and then never completes the TLS one. A fallback
    that only walked ``connect_tcp`` would bind to it, hand it up, and lose the message exactly
    as production did."""
    network = fake_network(tls_dead=[V6])
    with caplog.at_level(logging.WARNING, logger="cogno_gateway.net"):
        result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is True and result.message_ids == ["1"]
    # both halves of the bind were walked: two TCP opens, two handshakes, IPv6 then IPv4
    assert network.tried == [V6, V4]
    assert [address for address, _sni in network.handshakes] == [V6, V4]
    # the cost, on the virtual clock: ONE connect budget, not the request's whole budget
    assert network.elapsed == DEFAULT_CONNECT_TIMEOUT
    assert network.elapsed < 10.0
    assert network.resolutions == 1

    fallback = [r.getMessage() for r in caplog.records if "event=ip_family_fallback" in r.getMessage()]
    assert fallback == ["event=ip_family_fallback phase=tls family=ipv4 attempts=2 after=1"]


async def test_the_certificate_is_still_checked_against_the_provider_name(fake_network):
    """The property that makes picking the address ourselves safe at all.

    We connect to a literal IP, so the obvious wrong implementation — rewrite the URL to the
    address — would send no SNI and verify the certificate against an IP. httpcore takes
    ``server_hostname`` from the ORIGIN and this module never touches it, so both handshakes name
    the provider. Asserted on the FALLBACK path, because that is where a hand-rolled reconnect
    would be tempted to invent its own."""
    network = fake_network(tls_dead=[V6])
    await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert network.handshakes == [(V6, "api.telegram.org"), (V4, "api.telegram.org")]


async def test_a_dead_ipv6_connect_also_falls_through(fake_network):
    """The other half of the same walk: an address that refuses the TCP connect outright.

    Cheaper for us than the handshake case (nothing was ever bound) and it must land in the same
    place — one wasted budget, the message delivered on IPv4."""
    network = fake_network(tcp_dead=[V6])
    result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is True
    assert network.tried == [V6, V4]
    assert [address for address, _sni in network.handshakes] == [V4]
    assert network.elapsed == DEFAULT_CONNECT_TIMEOUT


# ── TWIN 2 — both families answer: the first, and WHICH first ───────────────────
async def test_when_both_families_answer_the_first_one_is_used_and_named(fake_network, caplog):
    """No fallback happens, and the order is OURS.

    "The first" is only a testable claim if the ordering does not come from the box's resolver
    configuration, so the assertion is on the address that was tried — the documentation IPv6 one
    — and not on a count. A second run of the same suite on a machine with a different
    ``gai.conf`` gets the same answer."""
    network = fake_network()
    with caplog.at_level(logging.DEBUG, logger="cogno_gateway.net"):
        result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is True
    assert network.tried == [V6]                 # one attempt: nothing to fall back from
    assert network.handshakes == [(V6, "api.telegram.org")]
    assert not [r for r in caplog.records if "event=ip_family_fallback" in r.getMessage()]
    bound = [r.getMessage() for r in caplog.records if "event=ip_family_bound" in r.getMessage()]
    assert bound == ["event=ip_family_bound phase=tcp family=ipv6 resolutions=1"]


async def test_the_operator_can_pick_the_family_and_the_other_is_never_tried(fake_network,
                                                                             monkeypatch):
    """``COGNO_HTTP_IP_FAMILY=ipv4`` — the same two live addresses, the other answer.

    It is the discriminating half of the twin above: if the order came from the OS rather than
    from this library, both tests could not pass on the same machine."""
    monkeypatch.setenv(net.FAMILY_ENV, "ipv4")
    network = fake_network()
    result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is True
    assert network.tried == [V4]
    assert V6 not in network.tried


# ── TWIN 3 — no family answers: a NAMED error that says which ones were tried ───
async def test_when_no_family_answers_the_error_names_the_families(fake_network, no_backoff):
    """An error that does not say which family failed makes the next person guess.

    The message travels through ``_error_detail`` — the channel this repo already has for the
    reason a send failed — rather than through a second one invented here, and it survives httpx
    re-raising httpcore's exception as its own because it is in the MESSAGE and not in the class."""
    network = fake_network(tls_dead=[V6], tcp_dead=[V4])
    result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is False
    assert result.error.startswith(FAMILIES_EXHAUSTED)
    assert "ipv6" in result.error and "ipv4" in result.error
    assert "ConnectTimeout" in result.error
    # the walk is bounded: two families per HTTP call, and the existing per-call retry (#16) makes
    # exactly one more — 4 attempts, 4 × the CONNECT budget, never the read budget
    assert network.tried == [V6, V4, V6, V4]
    assert network.elapsed == 4 * DEFAULT_CONNECT_TIMEOUT
    assert no_backoff.slept, "the exhausted walk is a transport failure, so #16 still retries once"


async def test_a_name_with_no_address_at_all_is_still_a_named_error(fake_network):
    """The empty resolution. It has no family to name, so it says so instead of saying nothing —
    an empty parenthesis would read as a bug in the error rather than a fact about the name."""
    fake_network(addresses=())
    result = await TelegramChannel(CFG).send("42", OutboundMessage(text="oi"))

    assert result.ok is False
    assert result.error == f"{FAMILIES_EXHAUSTED} (no address resolved)"


# ── the denominator: what one message costs in name lookups ────────────────────
async def test_one_name_lookup_per_message_however_many_chunks_it_becomes(fake_network):
    """The probe the owner asked for, with its denominator: resolutions per MESSAGE.

    A long reply becomes several provider calls, and the question "what is this connecting to,
    and how often" is only answerable against that count. The connection is pooled and the family
    walk happens once at bind time, so the answer is ONE lookup for N calls — a number that would
    become N the day somebody moved the client inside the chunk loop."""
    network = fake_network()
    text = "x " * 800
    result = await TelegramChannel(CFG).send("42", OutboundMessage(text=text))

    chunks = split_message(text, max_chars=600)
    assert len(chunks) > 1, "the fixture must actually chunk for this count to mean anything"
    assert result.ok is True and len(result.message_ids) == len(chunks)
    assert network.resolutions == 1
    assert network.tried == [V6]        # one bind carried every chunk


async def test_a_literal_address_is_never_looked_up(fake_network):
    """An Evolution instance configured by IP costs zero lookups — there is nothing to resolve
    and no family to choose. The counter above would be a lie if this path quietly resolved."""
    network = fake_network()
    backend = FamilyFallbackBackend(resolver=network.resolve, opener=network.open)
    await backend.connect_tcp(V4, 443)

    assert network.resolutions == 0
    assert network.tried == [V4] and backend.chosen == IPV4


# ── the ordering, as a unit ────────────────────────────────────────────────────
def test_one_address_per_family_in_this_librarys_order():
    """Two decisions in one line, and both are deliberate.

    ONE address per family: the measured defect is family-level, and walking every A record of a
    many-homed provider multiplies the wait in exactly the way a retry over a dead family does.
    OUR order: ``auto`` keeps IPv6 first, which is what the OS and anyio already prefer, so a
    healthy network keeps the path it has today."""
    resolved = [(AF_INET6, V6), (AF_INET6, "2001:db8::2"), (AF_INET, V4), (AF_INET, "192.0.2.2")]

    assert order_candidates(resolved, family=AUTO) == [Candidate(IPV6, V6), Candidate(IPV4, V4)]
    assert order_candidates(resolved, family=IPV4) == [Candidate(IPV4, V4)]
    assert order_candidates(resolved, family=IPV6) == [Candidate(IPV6, V6)]


def test_a_family_the_name_does_not_have_is_simply_absent():
    """An IPv4-only name under ``auto`` yields one candidate, not a candidate that cannot work."""
    assert order_candidates([(AF_INET, V4)], family=AUTO) == [Candidate(IPV4, V4)]
    assert order_candidates([(AF_INET, V4)], family=IPV6) == []


def test_an_unknown_address_family_is_dropped_rather_than_guessed():
    """``getaddrinfo`` can hand back families this library has no order for (``AF_UNIX``, and
    whatever a future stack adds). Dropping one is safe; guessing which of our two it resembles
    is not."""
    assert order_candidates([(1, "/tmp/sock"), (AF_INET, V4)], family=AUTO) == [
        Candidate(IPV4, V4)]


# ── the configuration, and what it refuses to do ───────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("", AUTO), ("   ", AUTO), ("auto", AUTO),
    ("ipv4", IPV4), ("IPv6", IPV6), (" ipv4 ", IPV4),
    ("v4", AUTO), ("yes", AUTO), ("4", AUTO),
])
def test_the_family_policy_never_raises_on_a_typo(raw, expected):
    """A misconfigured environment variable falls to ``auto``.

    The alternative — refusing to build a client — would let one typo lose every contact's reply,
    which is a worse defect than the stall this module fixes."""
    assert ip_family({net.FAMILY_ENV: raw}) == expected


@pytest.mark.parametrize("raw,expected", [
    ("", DEFAULT_CONNECT_TIMEOUT), ("2.5", 2.5), ("10", 10.0),
    ("0", DEFAULT_CONNECT_TIMEOUT), ("-1", DEFAULT_CONNECT_TIMEOUT),
    ("soon", DEFAULT_CONNECT_TIMEOUT),
])
def test_the_connect_budget_refuses_a_value_that_would_fail_every_send(raw, expected):
    """Zero and negative are refused with the same reason as garbage: a connect budget of zero
    does not mean "be fast", it means "never connect"."""
    assert connect_timeout({net.CONNECT_TIMEOUT_ENV: raw}) == expected


# ── the two budgets, separated ─────────────────────────────────────────────────
def test_connect_and_read_are_separate_budgets():
    """The second half of the fix, and the one the fallback depends on: without a short connect
    budget the walk is bounded by the READ budget and a dead family costs the whole request."""
    client = build_async_client(ChannelConfig(token="t", timeout=30.0))

    assert client.timeout.read == 30.0
    assert client.timeout.write == 30.0
    assert client.timeout.connect == DEFAULT_CONNECT_TIMEOUT
    assert client.timeout.connect < client.timeout.read


def test_a_caller_asking_for_less_than_the_leash_gets_less():
    """``min`` and not a constant: a channel configured with a 2 s budget must not be handed a 5 s
    connect, which would let one address outlast the whole call it belongs to."""
    client = build_async_client(ChannelConfig(token="t", timeout=2.0))

    assert client.timeout.connect == 2.0 and client.timeout.read == 2.0


def test_the_leash_is_configurable(monkeypatch):
    monkeypatch.setenv(net.CONNECT_TIMEOUT_ENV, "1.5")
    client = build_async_client(ChannelConfig(token="t", timeout=30.0))

    assert client.timeout.connect == 1.5 and client.timeout.read == 30.0


# ── the premise, pinned so it cannot rot in silence ────────────────────────────
def test_httpx_still_exposes_the_pool_this_fallback_installs_into():
    """httpx does not offer the connection pool's network backend as an argument, so it is
    installed after construction. That private shape is a PREMISE of this module, and a premise
    is worth a test of its own: the day an httpx upgrade moves it, the failure should be this
    line rather than a channel that quietly stops falling back."""
    transport = httpx.AsyncHTTPTransport()

    assert hasattr(transport, "_pool")
    assert hasattr(transport._pool, "_network_backend")


def test_the_transport_really_installs_the_backend():
    """...and the installation is not merely attempted. ``FamilyFallbackTransport`` degrades to a
    stock transport with a WARNING if the shape ever changes, so "it constructed fine" proves
    nothing on its own — this asserts the pool is actually using it."""
    transport = FamilyFallbackTransport()

    assert isinstance(transport.backend, FamilyFallbackBackend)
    assert transport._pool._network_backend is transport.backend


def test_every_adapter_reaches_the_provider_through_the_one_constructor():
    """Derived from the package, in the mould of the markup guards next door.

    A fifth channel is exactly the one nobody would remember to wire, and it would ship with the
    single timeout and no family fallback while the suite stayed green. The rule is therefore
    mechanical: ``net.py`` is the only module in this package that may construct an
    ``httpx.AsyncClient``."""
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[2] / "cogno_gateway"
    direct = [f"{path.name}:{number}"
              for path in sorted(package.glob("*.py")) if path.name != "net.py"
              for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
              if "httpx.AsyncClient(" in line]

    assert not direct, ("an outbound path builds its own client, so it gets neither the split "
                        "timeouts nor the family fallback: " + "; ".join(direct))


def test_the_guard_above_has_a_subject():
    """Its negative half. The scan asserts an absence, and a renamed constructor would empty the
    list and read as green — so the files that DO go through it are named."""
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[2] / "cogno_gateway"
    users = [path.name for path in sorted(package.glob("*.py"))
             if path.name != "net.py" and "build_async_client(" in path.read_text(encoding="utf-8")]

    assert users == ["cloud.py", "evolution.py", "provisioning.py", "telegram.py"]


# ── the plain-HTTP path: no handshake to fall back on, and a wrapper that must not eat bytes ──
async def test_a_plain_http_channel_binds_at_the_tcp_phase_and_carries_its_bytes(fake_network,
                                                                                 caplog):
    """An Evolution instance on ``http://`` never calls ``start_tls``, so the stream this module
    hands back is the one the whole request is read and written through.

    That makes it the case where the wrapper's delegation is load-bearing rather than incidental:
    every https send drops the wrapper the moment the handshake succeeds and never touches it
    again, so a broken ``read`` would be invisible in every other test in this file and would
    break every self-hosted WhatsApp instance in production."""
    from cogno_gateway import EvolutionChannel

    network = fake_network(body=EVOLUTION_OK)
    channel = EvolutionChannel(ChannelConfig(base_url="http://evo.invalid/", token="K",
                                             instance="i1"))
    with caplog.at_level(logging.DEBUG, logger="cogno_gateway.net"):
        result = await channel.send("5500000000000@s.whatsapp.net", OutboundMessage(text="oi"))

    assert result.ok is True and result.message_ids == ["m1"]
    assert network.handshakes == [], "there is no TLS on http:// — nothing to hand back to"
    bound = [r.getMessage() for r in caplog.records if "event=ip_family_bound" in r.getMessage()]
    assert bound == ["event=ip_family_bound phase=tcp family=ipv6 resolutions=1"]


async def test_a_plain_http_channel_still_falls_back(fake_network):
    """...and the walk is the same one. The wrapper is only about the TLS half; the TCP half was
    already covered inside ``connect_tcp``, and an http:// channel must not lose it."""
    from cogno_gateway import EvolutionChannel

    network = fake_network(body=EVOLUTION_OK, tcp_dead=[V6])
    channel = EvolutionChannel(ChannelConfig(base_url="http://evo.invalid/", token="K",
                                             instance="i1"))
    result = await channel.send("5500000000000@s.whatsapp.net", OutboundMessage(text="oi"))

    assert result.ok is True
    assert network.tried == [V6, V4]


# ── the degradations: loud, never silent, never fatal ─────────────────────────
def test_a_transport_without_the_pool_shape_degrades_loudly_instead_of_raising(caplog):
    """The branch that decides whether this fix can ship INERT.

    If a future httpx moves the pool, the honest outcomes are two: take every send down to
    protect a fallback, or keep sending without it. This picks the second and pays for it with a
    WARNING, because a channel that cannot send is a worse defect than a channel that cannot fall
    back — but a fallback that goes missing QUIETLY is worse than both, which is why the line is
    asserted here and not merely intended."""
    net._warned.discard("pool-shape")
    backend = FamilyFallbackBackend()

    class _NoPool:
        pass

    with caplog.at_level(logging.WARNING, logger="cogno_gateway.net"):
        installed = net.install_backend(_NoPool(), backend)

    assert installed is False
    assert [r.getMessage() for r in caplog.records
            if "event=ip_family_unavailable" in r.getMessage()]


def test_a_configuration_complaint_is_made_once_per_process(caplog):
    """``ip_family`` is read on the way into every send, so a typo would otherwise write a WARNING
    per message — the shape that turns a log into noise and a noisy log into an ignored one."""
    net._warned.discard("family:v4")
    with caplog.at_level(logging.WARNING, logger="cogno_gateway.net"):
        assert ip_family({net.FAMILY_ENV: "v4"}) == AUTO
        assert ip_family({net.FAMILY_ENV: "v4"}) == AUTO
        assert ip_family({net.FAMILY_ENV: "v4"}) == AUTO

    assert len([r for r in caplog.records if "event=ip_family_invalid" in r.getMessage()]) == 1


# ── the real resolver, on numeric input only (it parses; it sends no packet) ───
async def test_the_real_resolver_returns_pairs_the_ordering_can_read():
    """The one place the shipped resolver runs, and it runs on LITERAL addresses — ``getaddrinfo``
    parses those without asking anybody, so this makes no DNS query and opens no socket.

    Its subject is the contract between the two halves: the resolver yields ``(family, address)``
    and the ordering reads exactly that. A resolver that returned httpx's 5-tuple, or the sockaddr
    instead of its first element, would leave every ordering test above green and every real send
    broken."""
    from cogno_gateway.net import resolve_addresses

    assert order_candidates(await resolve_addresses("127.0.0.1", 443)) == [
        Candidate(IPV4, "127.0.0.1")]
    assert order_candidates(await resolve_addresses("::1", 443)) == [Candidate(IPV6, "::1")]
