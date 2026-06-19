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

## Decoupled from cognition & audio

The gateway imports neither `cogno-anima` nor `cogno-vox`. Inbound audio comes back as **bytes** (`fetch_media`) for the host to run through vox STT; a voice reply is just `OutboundMessage(audio=tts_bytes)`. The host wires the two edges to the pipeline.

## WhatsApp: two providers, one port

WhatsApp is pluggable — the host picks per tenant:

- **`EvolutionChannel`** (`"evolution"`) — Evolution API, unofficial (QR/Baileys), free, full-featured; good for dev/testing.
- **`WhatsAppCloudChannel`** (`"whatsapp_cloud"`) — the **official Meta Cloud API**, for production/compliance: HMAC webhook verification, free-form replies within the 24h service window, and **template** messages for proactive sends outside it (`OutboundMessage(template=Template(...))`).

## Install

```bash
pip install cogno-gateway          # adapters talk to providers over httpx; no web framework
pip install -e ".[dev]"            # tests (provider calls are mocked — no network)
```

## Test

```bash
pytest tests/unit -q
```
