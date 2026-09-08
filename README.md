# cogno-gateway

**Messaging transport edge for the [Cogno](https://github.com/sudoers-ai/cogno-anima) cognitive pipeline** — Telegram / WhatsApp (Evolution) / Web channel adapters behind a normalized, content-typed message model.

`cogno-gateway` is the **sibling of [`cogno-vox`](https://github.com/sudoers-ai/cogno-vox)**: where vox converts audio ⇆ text, gateway converts **channel messages ⇆ a normalized model**. Pure transport — it verifies a webhook, parses the provider payload, fetches media, and sends a reply. It does **not** orchestrate the pipeline, read a database, or run an HTTP server.

> Status: **alpha** — Telegram, WhatsApp (Evolution), and Web channels + the unit suite are in place.

```
webhook → verify + parse_inbound → [audio? fetch_media → vox STT] → PIPELINE (host)
        → reply → [vox TTS] → send
```

## One port, pluggable channels

Every adapter satisfies the `Channel` protocol — `verify` · `parse_inbound` · `fetch_media` · `send` — so the host treats Telegram, WhatsApp, and Web uniformly:

```python
from cogno_gateway import create_channel, ChannelConfig, OutboundMessage

tg = create_channel("telegram", ChannelConfig(token=bot_token, secret=hook_secret))
wa = create_channel("whatsapp", ChannelConfig(base_url=evo_url, token=apikey, instance="tenant1"))
web = create_channel("web")     # the cogno-cloud-ui {session_id, message} contract

msg = tg.parse_inbound(payload)                 # → InboundMessage (content-typed)
await tg.send(msg.sender, OutboundMessage(text="resposta"))   # auto-chunked
```

## Rich, content-typed messages

`InboundMessage.kind` is a `MessageKind`: `TEXT · IMAGE · AUDIO · VIDEO · DOCUMENT · LOCATION · REACTION · STICKER · INTERACTIVE`. So a host handles **reactions** (emoji + target message id), **media** (a `MediaRef` resolved lazily via `fetch_media` — e.g. to feed audio to cogno-vox), **quick-reply buttons** (send `OutboundMessage(buttons=[Button(...)])`; the tap returns `kind=INTERACTIVE` with `selection.id`), replies, and plain text uniformly across channels.

## Markup per channel, decided once

Bold is written differently on every transport: `*one asterisk*` on WhatsApp, `**two**` in
markdown, and nothing at all on a surface that renders no markup. Write markdown and the adapter
converts on the way out (`to_channel_markup`) — the gateway is the only layer that knows which
channel the text is going to. It is a delimiter swap, never a markdown renderer: arithmetic,
globs and unpaired markers reach the contact exactly as written. Each cell of the table carries
the reason it holds the value it does, so it can be turned when the surface changes.

## Decoupled from cognition & audio

The gateway imports neither `cogno-anima` nor `cogno-vox`. Inbound audio comes back as **bytes** (`fetch_media`) for the host to run through vox STT; a voice reply is just `OutboundMessage(audio=tts_bytes)`. The host wires the two edges to the pipeline.

## WhatsApp: two providers, one port

WhatsApp is pluggable — the host picks per tenant:

- **`EvolutionChannel`** (`"evolution"`) — Evolution API, unofficial (QR/Baileys), free, full-featured; good for dev/testing.
- **`WhatsAppCloudChannel`** (`"whatsapp_cloud"`) — the **official Meta Cloud API**, for production/compliance: HMAC webhook verification, free-form replies within the 24h service window, and **template** messages for proactive sends outside it (`OutboundMessage(template=Template(...))`).

## Pairing a WhatsApp account (Evolution QR)

`EvolutionChannel` talks to an instance that already exists. `cogno_gateway.provisioning` is the
half that **brings one into existence**: create it, hand back the QR to scan, poll the connection
state, keep the return address alive, and tear it down.

```python
from cogno_gateway import EvolutionWhatsAppProvisioner

prov = EvolutionWhatsAppProvisioner(
    base_url=evo_url, api_key=apikey,
    webhook_base="https://my.app",                       # the public URL YOU serve
    webhook_secret=secret,                               # → webhook headers.apikey
    instance_template="myapp_{account}",                 # your naming, not ours
    webhook_path_template="/webhook/whatsapp_evo-{account}")

conn = await prov.connect("acct-42")          # → WhatsAppConnection(qrcode_base64=…, status=…)
st   = await prov.status("acct-42")           # → WhatsAppStatus(state="open", webhook_ok=…)
cfg  = ChannelConfig(**prov.channel_credentials("acct-42"))   # → a send-capable EvolutionChannel
```

`account` is an **opaque key you choose** — what it means (a tenant, a workspace, one user), how
its instance is named and which URL you serve are your decisions, so they arrive as templates
rather than as assumptions baked in here. The defaults are the identity mapping.

`status()` also probes the **return address**, which the connection state says nothing about: an
instance sits at `"open"` while every inbound message is delivered to a dead URL. It asks the
*provider* where it will deliver (never our own store — that held the right URL all along), and
re-points it when it is stale, but **only after proving your public URL answers** — an unguarded
heal during an outage rewrites every account's webhook to a dead address. `InMemoryWhatsAppProvisioner`
is the deterministic stub for dev/tests.

## Install

```bash
pip install cogno-gateway          # adapters talk to providers over httpx; no web framework
pip install -e ".[dev]"            # tests (provider calls are mocked — no network)
```

## The Cogno ecosystem

`cogno-gateway` is one organ of **[Cogno](https://github.com/sudoers-ai)** — a family of
small, composable, Apache-2.0 libraries that together form a complete
conversational-agent platform. Each library owns a single concern and stays
infra-agnostic; a **host** assembles them into a running agent:

![The Cogno ecosystem](docs/assets/cogno-ecosystem.svg)

The open-source libraries are the organs; the **host is the body** that joins
them. Our reference host — `cogno-host`, with its `cogno-ui` dashboard — is the
private product layer, but it holds no special powers: everything it does rides
on the public seams documented in each library's `docs/HOST_INTEGRATION.md`, so
you can assemble a body of your own.

## Test

```bash
pytest tests/unit -q
```
