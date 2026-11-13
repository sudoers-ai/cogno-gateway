"""``EvolutionChannel.group_participants`` — the two reads a host asks beside a room's registration
to learn the bot's own LID for a WhatsApp @mention.

The shapes here are COPIED from a real capture of both reads: the ``fetchInstances`` body is a
LIST of FLAT items, each with a
``name`` and an ``ownerJid`` (``<digits>@s.whatsapp.net``, no ``:<n>``) beside a ``token`` (a secret)
and a ``number``; the ``findGroupInfos`` body is ONE flat object carrying ``subject`` (the room's
name, third-party text — never read or logged) and ``participants: [{"id": "<lid>@lid",
"phoneNumber": "<phone>@s.whatsapp.net", …}]``. Every id, number, token and name is synthetic.

What these twins pin:

* the chain — the ``ownerJid`` of every fetchInstances item whose ``name == instance`` is returned
  as a tuple, and the single real one equals the bot member's ``phoneNumber`` (so the host can
  match member id → ``bot_lid``);
* the host, not the gateway, applies «exactly one, or nothing» — the gateway surfaces the raw
  candidates (0, 1 or more) so the host can tell a 0-match from an ambiguous >1;
* ``token`` and ``number`` are never read, returned or logged;
* any failure (non-2xx, unreadable, transport error) yields ``((), ())`` — the host stores no
  ``bot_lid`` and a mention cannot fire.
"""

from __future__ import annotations

import httpx

from cogno_gateway import ChannelConfig, EvolutionChannel
from tests.conftest import FakeResponse

INSTANCE = "cogno_acme"
OWNER = "5511900000002@s.whatsapp.net"            # the bot's ownerJid == its phoneNumber
BOT_LID = "200000000000002@lid"
AUTHOR_LID = "100000000000001@lid"
AUTHOR_PHONE = "5511900000001@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
SECRET_TOKEN = "SECRET-TOKEN-NEVER-LOG"
SUBJECT = "Grupo Demo Inventado"            # the room's name — third-party text, never read/logged

# fetchInstances → a LIST of FLAT items (real shape); the item carries token (secret) + number.
FI_ONE = [{"id": "uuid-1", "name": INSTANCE, "connectionStatus": "open", "ownerJid": OWNER,
           "number": None, "token": SECRET_TOKEN, "clientName": "c"}]
# two items that both answer to the instance name — ambiguous.
FI_TWO = [dict(FI_ONE[0]), dict(FI_ONE[0], id="uuid-2", ownerJid="5511999999999@s.whatsapp.net")]
# one item, but for a DIFFERENT instance.
FI_OTHER = [dict(FI_ONE[0], name="cogno_other")]
# the nested {instance:{instanceName,…}} shape, tolerated beside the flat one.
FI_NESTED = [{"instance": {"instanceName": INSTANCE, "ownerJid": OWNER, "token": SECRET_TOKEN}}]

# findGroupInfos → ONE flat object: subject (the room name) + participants (real shape).
GROUP_INFO = {"id": GROUP, "subject": SUBJECT, "size": 2, "owner": AUTHOR_LID,
              "participants": [
                  {"id": AUTHOR_LID, "phoneNumber": AUTHOR_PHONE, "admin": "superadmin"},
                  {"id": BOT_LID, "phoneNumber": OWNER, "admin": None}]}


def _chan() -> EvolutionChannel:
    return EvolutionChannel(ChannelConfig(base_url="http://evo.invalid:8080/", token="APIKEY",
                                          instance=INSTANCE))


def _routes(fake, *, fetch=None, participants=None):
    fake.routes = {}
    if fetch is not None:
        fake.routes["fetchInstances"] = fetch
    if participants is not None:
        fake.routes["group/findGroupInfos"] = participants


async def test_the_happy_chain_owner_candidate_and_members(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_ONE), participants=FakeResponse(GROUP_INFO))
    owners, members = await _chan().group_participants(GROUP)
    assert owners == (OWNER,)                         # exactly one candidate named this instance
    assert members == ((AUTHOR_LID, AUTHOR_PHONE), (BOT_LID, OWNER))
    # the chain the host closes: the owner equals the bot member's phoneNumber → that member's id
    bot = [mid for (mid, phone) in members if phone == owners[0]]
    assert bot == [BOT_LID]
    # the NOSSO filter rides on the request — name == self._instance
    fi_call = [c for c in fake_httpx.calls if "fetchInstances" in c["url"]][0]
    assert fi_call["params"] == {"instanceName": INSTANCE}


async def test_two_items_with_the_instance_name_surface_two_candidates(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_TWO), participants=FakeResponse(GROUP_INFO))
    owners, members = await _chan().group_participants(GROUP)
    assert len(owners) == 2                           # the host reads this as ambiguous — never guesses
    assert members == ((AUTHOR_LID, AUTHOR_PHONE), (BOT_LID, OWNER))


async def test_a_different_instance_name_yields_no_candidate(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_OTHER), participants=FakeResponse(GROUP_INFO))
    owners, _members = await _chan().group_participants(GROUP)
    assert owners == ()


async def test_the_nested_instance_shape_is_tolerated(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_NESTED), participants=FakeResponse(GROUP_INFO))
    owners, _members = await _chan().group_participants(GROUP)
    assert owners == (OWNER,)


async def test_fetch_instances_non_2xx_yields_no_candidate(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(status=500), participants=FakeResponse(GROUP_INFO))
    owners, members = await _chan().group_participants(GROUP)
    assert owners == () and members == ((AUTHOR_LID, AUTHOR_PHONE), (BOT_LID, OWNER))


async def test_participants_non_2xx_yields_empty_members(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_ONE), participants=FakeResponse(status=404))
    owners, members = await _chan().group_participants(GROUP)
    assert owners == (OWNER,) and members == ()


async def test_a_transport_failure_yields_empty_everything_and_logs_no_secret(fake_httpx, caplog):
    _routes(fake_httpx, fetch=httpx.ConnectError("down"), participants=FakeResponse(GROUP_INFO))
    with caplog.at_level("WARNING", logger="cogno_gateway.evolution"):
        owners, members = await _chan().group_participants(GROUP)
    assert (owners, members) == ((), ())
    assert "group_participants_failed" in caplog.text
    assert "ConnectError" in caplog.text            # the class, never the message or any secret


async def test_the_token_number_and_subject_are_never_in_the_return(fake_httpx):
    _routes(fake_httpx, fetch=FakeResponse(FI_ONE), participants=FakeResponse(GROUP_INFO))
    owners, members = await _chan().group_participants(GROUP)
    assert owners == (OWNER,)
    flat = repr((owners, members))
    # the fetchInstances secret/line and the room's third-party name never leave the method
    assert SECRET_TOKEN not in flat and "token" not in flat and "number" not in flat
    assert SUBJECT not in flat and "subject" not in flat
