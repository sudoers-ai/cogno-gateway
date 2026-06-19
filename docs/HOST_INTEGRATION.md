# Host Integration Guide

How to wire `cogno-gateway` into a real application. The library ships the
**channel transport** (verify · parse · fetch media · send); the host owns the
**HTTP endpoint, the per-tenant config, and the orchestration**. Companion to
`examples/host_min.py`.

> TL;DR — on a webhook: `channel.verify(...)` → `channel.parse_inbound(payload)` →
> (if audio, `channel.fetch_media(...)` → cogno-vox STT) → run the pipeline →
> (cogno-vox TTS) → `channel.send(sender, OutboundMessage(...))`.

---

## 1. The boundary

| Concern | Owner |
| --- | --- |
| Verify signature, parse provider payload, fetch media, send, chunk outbound | **gateway** |
| The HTTP endpoint (FastAPI/Flask/…), routing, background tasks | **host** |
| Per-tenant channel creds (bot token / Evolution apikey+instance / secret) | **host** |
| Running cognition; STT/TTS (cogno-vox); feedback policy | **host** |

The gateway imports neither `cogno-anima` nor `cogno-vox` — it's pure transport,
a sibling of vox. The host connects them.

---

## 2. Receiving (inbound)

```python
from cogno_gateway import create_channel, ChannelConfig, MessageKind

channel = create_channel("telegram", ChannelConfig(token=bot_token, secret=hook_secret))

# inside your webhook handler:
if not channel.verify(headers=request.headers, body=raw_body):
    return 401
msg = channel.parse_inbound(await request.json())
if msg is None:
    return 200                                  # echo / unsupported event — ignore

if msg.kind == MessageKind.AUDIO:
    audio = await channel.fetch_media(msg.media)   # → bytes → cogno-vox STT → text
elif msg.kind == MessageKind.REACTION:
    handle_feedback(msg.reaction.emoji, msg.reaction.target_message_id)   # host policy
else:
    text = msg.text
```

`InboundMessage` is content-typed (`MessageKind`): `TEXT`, `IMAGE`, `AUDIO`,
`VIDEO`, `DOCUMENT`, `LOCATION`, `REACTION`, `STICKER`, `INTERACTIVE`. Media is a
`MediaRef` (provider file id / URL) you resolve lazily with `fetch_media`;
reactions carry `emoji` + `target_message_id`; replies carry `reply_to`. A tapped
quick-reply / list option / inline button arrives as `kind=INTERACTIVE` with
`selection: ButtonReply(id, title)` — the `id` is the payload you sent.

---

## 3. Replying (outbound)

```python
from cogno_gateway import OutboundMessage, MediaRef

await channel.send(msg.sender, OutboundMessage(text=reply_text))               # text (auto-chunked)
await channel.send(msg.sender, OutboundMessage(audio=tts_bytes,                # voice note (vox TTS)
                                               audio_format="opus"))
await channel.send(msg.sender, OutboundMessage(media=[MediaRef(url="https://…/file.pdf")]))
await channel.send(msg.sender, OutboundMessage(reaction=Reaction("👍", msg.message_id)))

from cogno_gateway import Button                                                # quick-reply buttons
await channel.send(msg.sender, OutboundMessage(text="Confirmar agendamento?",
    buttons=[Button("confirm", "Sim ✅"), Button("cancel", "Não ❌")]))
```

Buttons render natively per channel — Telegram inline keyboard, WhatsApp Cloud
interactive buttons, Evolution `sendButtons`. The user's tap comes back as an
`INTERACTIVE` inbound carrying `selection.id` (`"confirm"` / `"cancel"`).

**More than 3 options?** Quick-reply buttons cap at 3 on WhatsApp; use a **list
menu** (up to ~10 rows). Same tap-back contract (`INTERACTIVE` + `selection.id`):

```python
from cogno_gateway import ListMenu, ListSection, Button
await channel.send(msg.sender, OutboundMessage(text="Escolha um serviço:",
    list_menu=ListMenu(button="Ver serviços", sections=[ListSection("Serviços", [
        Button("corte", "Corte"), Button("barba", "Barba"),
        Button("mani", "Manicure"), Button("massa", "Massagem")])])))
```

→ a WhatsApp list message / a Telegram inline keyboard (one row per option).

`send` chunks long text (`split_message`, default 600 chars / 6 chunks — override
via `ChannelConfig.max_chars`) and returns a `SendResult(ok, message_ids, error)`.
A transport failure is returned (`ok=False`), not raised, so the host decides.

---

## 4. Channels

| Channel | `kind` | Config | Notes |
| --- | --- | --- | --- |
| Telegram | `"telegram"` | `token` (bot), `secret` (webhook) | Bot API over httpx |
| WhatsApp (Evolution) | `"evolution"` / `"whatsapp"` | `base_url`, `token` (apikey), `instance`, `secret` | **unofficial** (QR/Baileys) — dev/testing |
| WhatsApp (Cloud) | `"whatsapp_cloud"` / `"cloud"` / `"meta"` | `token` (access token), `instance` (phone number id), `secret` (app secret), `extra["verify_token"]` | **official Meta Cloud API** — production/compliance |
| Web | `"web"` | `secret` (optional) | the cogno-cloud-ui `{session_id, message}` ⇄ `{session_id, response}` contract — `send` returns a dict via `serialize(...)` |

> **WhatsApp provider is pluggable** behind the `Channel` port: **Evolution** (free,
> unofficial) for dev, **Cloud API** (official) for production. The host picks per
> tenant.

### WhatsApp Cloud — reactive model & the 24h window

A user message opens a **24h customer-service window**. Inside it you reply
**free-form** (text/media/reaction), for free. A **proactive** message *outside*
the window (e.g. an appointment reminder) must use a pre-approved **template**:

```python
from cogno_gateway import OutboundMessage, Template

# inside the window (the user just messaged) — free-form:
await cloud.send(to, OutboundMessage(text="Confirmado para 3ª às 14h ✅"))

# outside the window (proactive reminder) — pre-approved template:
await cloud.send(to, OutboundMessage(template=Template(
    "appointment_reminder", lang="pt_BR", params=["3ª feira", "14h"])))
```

*Deciding* free-form vs template (is the window open?) is **host policy** — you
track the user's last-inbound timestamp. The adapter just supports both sends.
The GET webhook handshake is `cloud.verify_subscription(mode=, token=, challenge=)`;
the POST signature is `cloud.verify(headers=, body=)` (HMAC-SHA256).

---

## 5. Multi-tenant

`ChannelConfig` is host-injected per tenant (resolve creds from your DB/secrets
store and build the channel per request, or cache per tenant). The gateway never
reads a database, an env var, or a secret on its own.
