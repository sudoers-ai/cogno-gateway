"""Shared test doubles — a fake httpx.AsyncClient so adapter send/fetch paths
run with no network."""

import json as _json

import pytest


class FakeResponse:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status

    def json(self):
        return self._payload

    @property
    def text(self):
        return self.content.decode() if self.content else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            err = httpx.HTTPError(f"HTTP {self.status_code}")
            err.response = self   # mirror httpx.HTTPStatusError so error detail can be surfaced
            raise err


class Script:
    """A per-call sequence for one route: each call consumes the next entry, and the last entry
    repeats once the sequence is exhausted.

    It exists because a TRANSPORT failure is not a response — a timeout or a dropped connection
    is a ``raise`` out of the client, with no status code to canned-respond with — and because
    testing a retry needs the *first* call and the *second* call to differ. An entry that is an
    exception is raised; anything else is returned."""

    def __init__(self, *entries):
        assert entries, "a Script needs at least one entry"
        self._entries = list(entries)

    def take(self):
        return self._entries.pop(0) if len(self._entries) > 1 else self._entries[0]


class FakeAsyncClient:
    """Records calls; returns canned responses by URL substring. Configure via the
    class-level ``routes`` dict {url_substring: FakeResponse}; calls are appended to
    ``FakeAsyncClient.calls``. A route value may also be a :class:`Script` (a per-call
    sequence) or a bare exception (raised on every call — a transport failure)."""

    routes: dict = {}
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _respond(self, method, url, **kwargs):
        FakeAsyncClient.calls.append({"method": method, "url": url, **kwargs})
        for sub, resp in FakeAsyncClient.routes.items():
            if sub in url:
                if isinstance(resp, Script):
                    resp = resp.take()
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        return FakeResponse()

    async def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


@pytest.fixture
def fake_httpx(monkeypatch):
    """Patch httpx.AsyncClient everywhere with the recording fake. Returns the
    class so a test can set ``.routes`` and inspect ``.calls``."""
    import httpx
    FakeAsyncClient.routes = {}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def body_of(call) -> dict:
    """Extract the JSON body from a recorded call (json= or data=)."""
    if "json" in call:
        return call["json"]
    if "data" in call:
        return call["data"] if isinstance(call["data"], dict) else _json.loads(call["data"])
    return {}
