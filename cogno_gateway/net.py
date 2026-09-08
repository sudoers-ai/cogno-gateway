"""
cogno_gateway.net — how a channel's HTTP client REACHES the provider.

Every adapter in this package opens its own ``httpx.AsyncClient`` per ``send``/``fetch_media``.
Until now each one built it the same way and with the same single argument — one ``timeout``
covering every phase of the call — and left the address selection entirely to the stack below.
This module is the one place both of those decisions are made, and it exists because of a
measured stall: a reply that never left the box because the connection to the provider hung on
an IPv6 address that answered TCP and never finished the TLS handshake.

**What the stack below actually does, verified rather than assumed.** The claim "httpx has no
happy eyeballs" is half true, and the half that is false is the half that matters:

* ``httpx`` → ``httpcore`` → ``anyio.connect_tcp``, and *anyio* does implement RFC 6555: it
  resolves the name, puts an IPv6 address first, and starts the next address 250 ms later if
  the first has not connected. So the **TCP** connect is already raced across families.
* The **TLS handshake is not**. ``httpcore``'s connection calls ``connect_tcp`` and then, on
  the single stream that won that race, ``stream.start_tls(...)``. There is no second address
  left at that point and no step that goes back for one. An address that completes the TCP
  handshake and then black-holes the TLS one — a dead 6to4 path, a middlebox, a firewall that
  drops rather than rejects — wins the race precisely *because* it answered fast, and then
  hangs for the whole connect budget with the working IPv4 address never tried.

That is the failure this module is written against, so the fallback here has to cover **both
halves of the bind**, TCP *and* TLS. A fallback that only covered ``connect_tcp`` would be the
kind of fix that reads right, ships, and changes nothing for the message that was lost.

**Why not the alternatives.**

* *Shorten the timeout and let the existing retry handle it.* The retry
  (``TelegramChannel._post``) is per HTTP call and retries the same way, so it lands on the same
  IPv6 address the same ordering picked, and fails again — twice the wait, still no message.
  Shortening the timeout alone turns a long hang into a fast failure, which is better to watch
  and no better to receive.
* *Pin the address in ``/etc/hosts``.* That is what was done by hand, and it is not code: it
  does not travel with a deploy, it does not survive a provider changing its IPs, and nothing
  in a test can see it.
* *Force IPv4 for everyone.* It works and it throws away a working IPv6 path wherever there is
  one. It stays available as an operator decision — ``COGNO_HTTP_IP_FAMILY=ipv4`` — rather than
  as this library's opinion about somebody's network.
* *Monkeypatch ``socket.getaddrinfo``.* Process-global: it would silently reach every other
  library in the host, which is a much larger blast radius than the problem.

**The shape of the fallback.** One address per family, families in *our* declared order
(``auto`` = IPv6 then IPv4, which is the order the OS and anyio already prefer, so a healthy
network keeps the path it has today), each attempt bounded by a short **connect** budget that is
separate from the long **read** budget. The worst case is therefore ``families × connect``
(2 × 5 s), not one 15 s hang, and not one 15 s hang per retry.

One address per family and not every address: the defect measured is family-level, and walking
every A record of a many-homed provider multiplies the wait in exactly the way a retry over a
dead family does — the thing this module exists to stop. A single dead host inside a live family
is left to the existing per-call retry, which re-resolves and may well be handed a different one.

**Timeouts, separated.** ``ChannelConfig.timeout`` was passed to httpx as a bare number, which
httpx spreads over all four phases — connect, read, write and pool alike. Connect and read are
not the same risk: a provider taking its time to answer is normal and deserves the long budget,
an address that does not answer at all is not and deserves a short one. So a client built here
gets ``connect`` from :func:`connect_timeout` (default 5 s, ``COGNO_HTTP_CONNECT_TIMEOUT``,
never longer than the read budget) and leaves ``read`` where the caller set it.

**Where this sits relative to the retry.** Below it. The family walk happens inside a single
``connect_tcp``/``start_tls`` pair, so the first HTTP call already gets the working family; the
retry above only ever sees a call that has exhausted every family, and pays 2 × connect for it
rather than multiplying a dead family's wait.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import os
import socket
import ssl
import typing
from dataclasses import dataclass

import httpcore
import httpx

logger = logging.getLogger("cogno_gateway.net")

__all__ = [
    "AUTO",
    "IPV4",
    "IPV6",
    "VALID_IP_FAMILIES",
    "FAMILY_ENV",
    "CONNECT_TIMEOUT_ENV",
    "DEFAULT_CONNECT_TIMEOUT",
    "FAMILIES_EXHAUSTED",
    "Candidate",
    "FamilyFallbackBackend",
    "FamilyFallbackTransport",
    "build_async_client",
    "connect_timeout",
    "install_backend",
    "ip_family",
    "order_candidates",
    "resolve_addresses",
]

AUTO = "auto"
IPV4 = "ipv4"
IPV6 = "ipv6"

#: The closed vocabulary of :data:`FAMILY_ENV`. Anything else falls to ``auto`` — a typo in an
#: environment variable must not be able to take a channel's sends down.
VALID_IP_FAMILIES = (AUTO, IPV4, IPV6)

#: Which families to try, per policy, **in the order this library tries them**. ``auto`` keeps
#: IPv6 first: that is what the OS resolver and anyio already prefer, so a network where IPv6
#: works keeps the exact path it has today and only the *stall* is bounded differently.
_FAMILY_ORDER: dict[str, tuple[str, ...]] = {
    AUTO: (IPV6, IPV4),
    IPV6: (IPV6,),
    IPV4: (IPV4,),
}

#: Typed ``int`` and not ``socket.AddressFamily``: a resolver is a seam, and an injected one
#: hands back whatever the caller wrote — ``socket.AF_INET6`` is an int, and so is 10.
_FAMILY_NAME: dict[int, str] = {int(socket.AF_INET6): IPV6, int(socket.AF_INET): IPV4}

FAMILY_ENV = "COGNO_HTTP_IP_FAMILY"
CONNECT_TIMEOUT_ENV = "COGNO_HTTP_CONNECT_TIMEOUT"

#: Short by design and *only* for the connect phase. Long enough for a healthy handshake across
#: an ocean, short enough that a family that will never answer costs a few seconds instead of
#: the whole request budget.
DEFAULT_CONNECT_TIMEOUT = 5.0

#: The name in the error raised when no family connected. It travels in the exception MESSAGE
#: rather than in a new exception class on purpose: ``TelegramChannel._error_detail`` already
#: reports ``str(exc) or type(exc).__name__`` for a transport failure, and httpx re-raises
#: httpcore's exceptions as its own — a bespoke class would be flattened to ``ConnectError`` on
#: the way up, while the message survives intact. One channel for the diagnosis, not two.
FAMILIES_EXHAUSTED = "ip_family_exhausted"

Resolved = typing.Sequence[typing.Tuple[int, str]]
Resolver = typing.Callable[[str, int], typing.Awaitable[Resolved]]
Opener = typing.Callable[..., typing.Awaitable[httpcore.AsyncNetworkStream]]

_stock_backend = httpcore.AnyIOBackend()

#: Configuration values already complained about, so a misconfigured deployment says it once per
#: process instead of once per message.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: typing.Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


# ── configuration ────────────────────────────────────────────────────────────
def ip_family(env: typing.Mapping[str, str] | None = None) -> str:
    """The address-family policy: ``auto`` (default, IPv6 then IPv4), ``ipv4`` or ``ipv6``.

    Unset, blank or unrecognised → ``auto``. It never raises: this is read on the way into a
    send, and a typo in an environment variable losing a contact's reply would be a worse defect
    than the one this module fixes."""
    raw = (os.environ if env is None else env).get(FAMILY_ENV, "") or ""
    value = raw.strip().lower()
    if not value:
        return AUTO
    if value in VALID_IP_FAMILIES:
        return value
    _warn_once(f"family:{value}", "event=ip_family_invalid value=%s falling_back=%s", value, AUTO)
    return AUTO


def connect_timeout(env: typing.Mapping[str, str] | None = None) -> float:
    """The per-attempt **connect** budget in seconds (``COGNO_HTTP_CONNECT_TIMEOUT``).

    Unset or unparseable → :data:`DEFAULT_CONNECT_TIMEOUT`; a non-positive value is refused for
    the same reason, since a zero connect budget fails every send instantly."""
    raw = (os.environ if env is None else env).get(CONNECT_TIMEOUT_ENV, "") or ""
    if not raw.strip():
        return DEFAULT_CONNECT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        _warn_once(f"connect:{raw}", "event=connect_timeout_invalid value=%s falling_back=%s",
                   raw.strip(), DEFAULT_CONNECT_TIMEOUT)
        return DEFAULT_CONNECT_TIMEOUT
    return value


# ── candidates ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Candidate:
    """One address this library will try, and the family it belongs to."""

    family: str      # ipv6 | ipv4
    address: str     # a literal IP — never a name, so nothing below us resolves again


async def resolve_addresses(host: str, port: int) -> list[tuple[int, str]]:
    """Resolve ``host`` to ``(address_family, ip)`` pairs, off the event loop.

    ``loop.getaddrinfo`` and not ``socket.getaddrinfo``: the blocking call runs in the loop's
    executor, which is what keeps a slow DNS server from stalling every other turn the host is
    serving in the same process."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [(family, str(sockaddr[0])) for family, _type, _proto, _canon, sockaddr in infos]


def order_candidates(resolved: Resolved, *, family: str = AUTO) -> list[Candidate]:
    """Pick **one address per family**, in this library's declared order.

    Deliberately not the resolver's order: the twin that matters ("both families answer — which
    one did we use?") must not be a question about the host's ``gai.conf``. ``auto`` yields IPv6
    then IPv4; an explicit family yields only that one, and an empty list when the name has no
    address in it (which the caller reports as an exhausted walk, not as a silent success)."""
    first: dict[str, str] = {}
    for af, address in resolved:
        name = _FAMILY_NAME.get(af)
        if name is not None and name not in first:
            first[name] = address
    wanted = _FAMILY_ORDER.get(family, _FAMILY_ORDER[AUTO])
    return [Candidate(name, first[name]) for name in wanted if name in first]


def _literal_family(host: str) -> str:
    """The family of ``host`` when it is already a literal IP, else ``""``."""
    try:
        parsed = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return ""
    return IPV6 if parsed.version == 6 else IPV4


def _exhausted(failures: typing.Sequence[tuple[str, BaseException]]) -> httpcore.ConnectError:
    """The named error for "no family connected", carrying every family that was tried.

    An error that does not say WHICH family failed makes the next person guess, and the guess is
    the whole diagnosis here — ``ipv6: ConnectTimeout; ipv4: ConnectError`` says a dead IPv6 path
    plus a refused IPv4 one, while ``ipv6: ConnectTimeout`` alone says the walk never got past
    the first family. The host and the port are deliberately absent: an Evolution ``base_url`` is
    a tenant's own instance, and this string is surfaced in ``SendResult.error``."""
    detail = "; ".join(f"{family}: {type(exc).__name__}" for family, exc in failures)
    return httpcore.ConnectError(f"{FAMILIES_EXHAUSTED} ({detail or 'no address resolved'})")


async def _aclose_quietly(stream: httpcore.AsyncNetworkStream) -> None:
    try:
        await stream.aclose()
    except Exception:      # noqa: BLE001 — closing a stream we are already abandoning
        pass


# ── the backend ──────────────────────────────────────────────────────────────
class FamilyFallbackBackend(httpcore.AsyncNetworkBackend):
    """Bind a connection by walking address families, TCP **and** TLS.

    Installed under an ``httpx`` transport, so every adapter gets it without changing a line of
    its own call sites. One instance is built per client, which makes its counters per-message:
    :attr:`resolutions` is how many times a name was looked up while sending one message, and
    :attr:`attempts` is every address that was tried.

    The seams are the two module-level functions :func:`resolve_addresses` and :func:`_open_tcp`,
    replaceable per instance *and* patchable by name — the adapters build their client with no
    arguments, so a seam only reachable through a constructor is a seam no test of the real send
    path can use.
    """

    def __init__(self, *, family: str = AUTO,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 resolver: Resolver | None = None,
                 opener: Opener | None = None) -> None:
        self._family = family if family in VALID_IP_FAMILIES else AUTO
        self._connect_timeout = connect_timeout
        self._resolver: Resolver = resolver or resolve_addresses
        self._opener: Opener = opener or _open_tcp
        #: How many name lookups this client performed — the denominator for "what is a message
        #: costing us in DNS". A pooled connection is bound once, so a reply split into several
        #: provider calls resolves once, not once per chunk.
        self.resolutions = 0
        #: Every address tried, in order. ``len(attempts)`` is the fallback's real cost.
        self.attempts: list[Candidate] = []
        #: The family that finally carried the connection, ``""`` if none did.
        self.chosen = ""

    # -- httpcore.AsyncNetworkBackend ------------------------------------
    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
                          ) -> httpcore.AsyncNetworkStream:
        budget = self.budget(timeout)
        candidates = await self._candidates(host, port)
        failures: list[tuple[str, BaseException]] = []
        for index, candidate in enumerate(candidates):
            stream = await self._try_open(candidate, port, budget, local_address,
                                          socket_options, failures)
            if stream is None:
                continue
            self._bound(candidate, failures, phase="tcp")
            return _FallbackStream(self, stream, candidates, index, port, budget,
                                   local_address, socket_options, failures)
        raise _exhausted(failures)

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: typing.Iterable[httpcore.SOCKET_OPTION]
                                  | None = None) -> httpcore.AsyncNetworkStream:
        # No address family to choose — a unix socket has no IP at all.
        return await _stock_backend.connect_unix_socket(path, timeout=timeout,
                                                        socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await _stock_backend.sleep(seconds)

    # -- internals -------------------------------------------------------
    def budget(self, timeout: float | None) -> float:
        """The per-attempt budget: our short connect leash, or the caller's if it is shorter.

        ``min`` and not ``or``: a caller that asked for a 2 s connect gets 2 s, and a caller that
        asked for 15 s still gets the leash — the whole point is that no single address may spend
        the request's budget."""
        if timeout is None:
            return self._connect_timeout
        return min(timeout, self._connect_timeout)

    async def _candidates(self, host: str, port: int) -> list[Candidate]:
        literal = _literal_family(host)
        if literal:
            return [Candidate(literal, host)]     # already an address; nothing to choose
        self.resolutions += 1
        return order_candidates(await self._resolver(host, port), family=self._family)

    async def _try_open(self, candidate: Candidate, port: int, budget: float,
                        local_address: str | None,
                        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None,
                        failures: list[tuple[str, BaseException]],
                        ) -> httpcore.AsyncNetworkStream | None:
        """One TCP attempt. A failure is RECORDED and returned as ``None`` — the walk decides
        whether there is another family left, not this."""
        self.attempts.append(candidate)
        try:
            return await self._opener(candidate.address, port, timeout=budget,
                                      local_address=local_address,
                                      socket_options=socket_options)
        except Exception as exc:      # noqa: BLE001 — every transport failure is a next-family
            failures.append((candidate.family, exc))
            return None

    def _bound(self, candidate: Candidate, failures: typing.Sequence[object], *,
               phase: str) -> None:
        """Record — and say — which family carried the connection.

        ``phase`` is in the line because the bind has two halves and only one of them is the one
        that was hanging: ``tcp`` is the address answering at all, ``tls`` is the handshake
        completing on it. A trace that showed only the family could not tell "IPv6 was never
        reachable" from "IPv6 answered and then went quiet", which is the whole distinction the
        stack below us gets wrong."""
        self.chosen = candidate.family
        if failures:
            # WARNING because it is a RECOVERED condition (LOGGING.md): the message went out,
            # and this line is the only record that a family had to be abandoned to send it.
            logger.warning("event=ip_family_fallback phase=%s family=%s attempts=%d after=%d",
                           phase, candidate.family, len(self.attempts), len(failures))
        else:
            logger.debug("event=ip_family_bound phase=%s family=%s resolutions=%d",
                         phase, candidate.family, self.resolutions)


async def _open_tcp(address: str, port: int, timeout: float | None = None,
                    local_address: str | None = None,
                    socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
                    ) -> httpcore.AsyncNetworkStream:
    """Open one TCP connection to a literal address.

    ``address`` is always an IP by the time it gets here, so anyio's own RFC 6555 walk sees a
    single target and does no lookup of its own — which is what makes :attr:`
    FamilyFallbackBackend.resolutions` the whole DNS cost of a message rather than half of it."""
    return await _stock_backend.connect_tcp(address, port, timeout=timeout,
                                            local_address=local_address,
                                            socket_options=socket_options)


class _FallbackStream(httpcore.AsyncNetworkStream):
    """A bound stream that still remembers the families it did not need.

    It exists for one reason: the half of the bind that hangs is the TLS handshake, and that half
    runs *after* ``connect_tcp`` has returned. Everything else is delegation — this wrapper is
    dropped the moment ``start_tls`` succeeds, because the TLS stream it returns is the real
    one."""

    def __init__(self, backend: FamilyFallbackBackend, stream: httpcore.AsyncNetworkStream,
                 candidates: list[Candidate], index: int, port: int, budget: float,
                 local_address: str | None,
                 socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None,
                 failures: list[tuple[str, BaseException]]) -> None:
        self._backend = backend
        self._stream = stream
        self._candidates = candidates
        self._index = index
        self._port = port
        self._budget = budget
        self._local_address = local_address
        self._socket_options = socket_options
        self._failures = failures
        #: How many families had already failed when this stream was handed back. The handshake
        #: below re-announces the bind only if that number GREW — otherwise ``connect_tcp`` has
        #: already said everything there is to say, and a second line would double every
        #: connection in the trace.
        self._failures_at_bind = len(failures)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str) -> typing.Any:
        return self._stream.get_extra_info(info)

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        """Handshake on the bound address; on failure, reconnect on the next family and retry.

        ``server_hostname`` is passed through untouched, and that is the property that makes the
        whole approach safe: httpcore takes it from the ORIGIN, not from the address we chose, so
        certificate verification and SNI still name the provider even though the socket went to a
        literal IP we picked ourselves."""
        budget = self._backend.budget(timeout)
        stream: httpcore.AsyncNetworkStream | None = self._stream
        index = self._index
        while True:
            if stream is not None:
                try:
                    tls = await stream.start_tls(ssl_context, server_hostname, budget)
                except Exception as exc:   # noqa: BLE001 — a dead handshake is a next-family
                    self._failures.append((self._candidates[index].family, exc))
                    await _aclose_quietly(stream)
                else:
                    if len(self._failures) > self._failures_at_bind:
                        self._backend._bound(self._candidates[index], self._failures,
                                             phase="tls")
                    else:
                        self._backend.chosen = self._candidates[index].family
                    return tls
            index += 1
            if index >= len(self._candidates):
                raise _exhausted(self._failures)
            stream = await self._backend._try_open(self._candidates[index], self._port, budget,
                                                   self._local_address, self._socket_options,
                                                   self._failures)


# ── the client every adapter builds ──────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _shared_ssl_context() -> ssl.SSLContext | None:
    """One CA-loaded context for the whole process.

    Not a micro-optimisation added for its own sake: a fresh ``httpx`` transport reads the CA
    bundle off disk every time, ~33 ms measured here, and this library builds a client per
    ``send``. Returning ``None`` on any failure hands the decision back to httpx unchanged."""
    try:
        return httpx.create_ssl_context()
    except Exception:      # noqa: BLE001 — an httpx that no longer offers this builds its own
        return None


def install_backend(transport: typing.Any, backend: FamilyFallbackBackend) -> bool:
    """Put ``backend`` under ``transport``'s connection pool. ``False`` if the shape is not there.

    Its own function because the failure it describes is the one that would otherwise be
    invisible: an httpx whose pool no longer carries a network backend leaves this library
    WORKING and silently without the fallback, which is the exact "fix that ships inert" this
    whole change is written against. A separate function is a thing a test can hand a wrong shape
    to; a few lines inside a constructor is not."""
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        _warn_once("pool-shape", "event=ip_family_unavailable reason=httpx_pool_shape httpx=%s",
                   httpx.__version__)
        return False
    pool._network_backend = backend
    return True


class FamilyFallbackTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` transport whose connection pool binds through :class:`FamilyFallbackBackend`.

    httpx does not expose the pool's network backend, so it is installed on the pool after
    construction. That private attribute is pinned by a test rather than trusted, and if a future
    httpx changes the shape the transport still WORKS — it just works without the fallback, and
    says so at WARNING. A guard that took sends down to protect a fallback would be worse than
    the stall it guards against; a guard that goes quiet would be worse still.
    """

    def __init__(self, *, backend: FamilyFallbackBackend | None = None,
                 **kwargs: typing.Any) -> None:
        kwargs.setdefault("verify", _shared_ssl_context() or True)
        super().__init__(**kwargs)
        candidate = backend or FamilyFallbackBackend(family=ip_family(),
                                                     connect_timeout=connect_timeout())
        self.backend = candidate if install_backend(self, candidate) else None


def build_async_client(config: typing.Any = None, *, timeout: float | None = None,
                       **kwargs: typing.Any) -> httpx.AsyncClient:
    """The one constructor for this package's outbound HTTP clients.

    ``config`` is a ``ChannelConfig`` (its ``timeout`` is the READ budget) or ``None`` with an
    explicit ``timeout``. Everything else is passed through to ``httpx.AsyncClient``.

    Two things are decided here and nowhere else: the connect budget is separated from the read
    budget (see the module docstring — they are not the same risk), and the transport walks
    address families. ``httpx.AsyncClient`` is looked up on the module at call time, which is
    what lets the unit suite swap the whole client for a fake and lets the integration suite swap
    only the transport."""
    read = float(timeout if timeout is not None else getattr(config, "timeout", 15.0) or 15.0)
    connect = min(connect_timeout(), read)
    kwargs.setdefault("timeout", httpx.Timeout(connect=connect, read=read, write=read, pool=read))
    kwargs.setdefault("transport", FamilyFallbackTransport())
    return httpx.AsyncClient(**kwargs)
