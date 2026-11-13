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
| Outbound **markup** — markdown bold into the channel's own dialect | **gateway** |
| Outbound **structure** — which fields, which order, which grouping | **host** / its tools |
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

# ── Verification default (three states) ──
# `ChannelConfig.require_secret` defaults to `None` (unset) = the kind's SECURE DEFAULT:
#   * a PROVIDER webhook (Telegram / Evolution / WhatsApp Cloud) FAILS CLOSED — `verify()`
#     rejects an inbound delivery when no secret is configured, since anyone who learns the URL
#     could forge one;
#   * the WEB widget stays OPEN — it is the host's OWN auth/CORS-guarded chat route, with no
#     third party to share a secret with, so failing it closed would break the web chat for no
#     gain.
# An explicit `True`/`False` is HONOURED BY EVERY KIND: a deployment with no secret opts out with
# `ChannelConfig(..., require_secret=False)`; a host that wants the web widget verified sets
# `require_secret=True` on it. The verify() refusal line says how to opt out and never logs the
# secret. (Internally the posture is read via `ChannelConfig.secret_required(default=...)`.)
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

**Group chats: dropped unless you opt in, and only on Telegram.** By default a message you
receive is a 1:1 message: the Telegram adapter drops `group`/`supergroup` chats (message, button
tap and reaction), the Evolution adapter drops `@g.us` (message and presence), and the WhatsApp
Cloud adapter drops a message whose object carries a `group_id` (every kind; since 0.1.2).
`InboundMessage` carries seven group fields, keyword-only and at the value that IS the 1:1 case:

| field | default | means |
|---|---|---|
| `conversation_id` | `""` | where a reply goes (the group); `""` = reply to `sender`, as today |
| `author` | `""` | the member who wrote it; `""` = the sender wrote it |
| `mentions_bot` | `False` | the text @-mentions this bot (meaningful only in a group) |
| `reply_to_bot` | `False` | it replies to one of this bot's messages (only in a group) |
| `reply_to_message_id` | `""` | the provider id this message QUOTES (Evolution `data.contextInfo.stanzaId`), exposed so a host can decide `reply_to_bot` itself (since 0.1.9) |
| `mentioned_ids` | `()` | the LIDs this message @-mentions (Evolution `data.contextInfo.mentionedJid`), exposed so a host can decide `mentions_bot` against the bot's own LID (since 0.1.10) |
| `author_is_bot` | `False` | the author is a bot |
| `membership` | `None` | set only on a `MessageKind.MEMBERSHIP` message: THIS bot joined or left the room (0.1.6) |

They stay out of `repr` while at their default; `dataclasses.asdict` does carry them (a dataclass
field cannot leave it). `dataclasses.replace` carries them through, so normalising `sender` never
loses the group.

**Telegram fills them when you opt in (since 0.1.4).** Build the adapter with
`ChannelConfig(accept_groups=True, bot_username="@your_bot")` (the handle from `getMe`, with or
without the `@`, any case). The default, `False`, is 0.1.3 byte for byte. With it on, a group
`message` arrives with:

| field | value in a group |
|---|---|
| `sender` | the ROOM's `chat.id` (negative) — **not the member** |
| `conversation_id` | the same room id |
| `author` | the member's `from.id` (in a private chat with the bot, that is their `chat.id`) |
| `author_is_bot` | `from.is_bot` — Telegram can deliver another bot's messages to a bot with privacy mode off |
| `mentions_bot` | an entity calls THIS bot: `mention` `@your_bot`, `text_mention` of the bot's user, or `bot_command` `/cmd@your_bot`, read from `entities` or `caption_entities`. Offsets are UTF-16 code units. The text alone is never read. No `bot_username` → never |
| `reply_to_bot` | `reply_to_message.from` is this bot: by the id in the token's prefix, or, when the token has none, by the handle on a user marked `is_bot` |

`sender` is the room on purpose: Telegram numbers messages per chat, so deduplicating on
`(sender, message_id)` stays correct, and a reply sent to `sender` goes to the room. Resolve the
person from `author`. Still dropped with the switch on: a group button tap or reaction, a message
posted on behalf of a chat (`sender_chat`: an anonymous admin, a linked channel's post), a message
with no `from`, and a forum supergroup (`is_forum`). **What reaches a turn is yours to decide**:
which rooms are registered, which trigger counts, who may speak. The adapter only parses.

**The bot joined or left a room (Telegram, since 0.1.6, with the same switch on).** A message of
kind `MessageKind.MEMBERSHIP` says it: `sender` and `conversation_id` are the room, `author` is
empty (who added the bot is not carried), and `membership` is a `Membership(status, title)` whose
`status` is `MEMBERSHIP_JOINED` or `MEMBERSHIP_LEFT` (the provider's `member`, `administrator`,
`restricted`, `left` and `kicked` are folded at parse) and whose `title` is the room's name (`""`
when absent; it is the room's text, never a log value). Two shapes produce it:

| shape | joined | left |
|---|---|---|
| `my_chat_member` (the bot's own status changed) | `member`, `administrator`, `restricted` with `is_member` | `left`, `kicked`, `restricted` without it |
| service message (every bot receives these, whatever its privacy mode) | `new_chat_members` naming the bot; `group_chat_created`; `migrate_from_chat_id` (a group upgraded to a supergroup gets a NEW id: this is the new room) | `left_chat_member` naming the bot; `migrate_to_chat_id` (the old room) |

A REMOVAL arrives only as `my_chat_member`: a kicked bot receives nothing more from that room.
`TelegramWebhookRegistrar` subscribes to it since 0.1.6, but a bot registered before keeps its old
subscription until its webhook is registered again (`setWebhook` replaces the record); until then
it still sees a join. Both shapes can arrive for the same join, so a host must handle a repeat.
Dropped: the switch off, a private chat or a channel, a forum supergroup, a member that is not
this bot, a service message about somebody else (that one parses as before, kind `UNKNOWN`).

**Can the bot read the room? (BotFather's privacy mode, since 0.1.6.)** With privacy mode ON,
Telegram delivers to a bot in a group ONLY the commands addressed to it (`/x@your_bot`), the
replies to its own messages and the service messages: an @mention alone never arrives. Ask with
`TelegramChannel.reads_all_group_messages()` or `TelegramWebhookRegistrar.reads_all_group_messages(
token=…)` — `getMe`'s `can_read_all_group_messages`: `False` = privacy ON, `True` = off, `None` =
unknown (a failed or partial answer), never a guess. The fix is the bot owner's: BotFather →
`/mybots` → the bot → Bot Settings → Group Privacy → Turn off.
**WhatsApp Cloud marks a group only inside the message.** Meta delivers a reply in a group the
business created through its Groups API under the same `messages` webhook field as a 1:1 message,
with a `group_id` in the message object and `from` set to the participant. Until 0.1.2 the adapter
did not read it, so such a reply arrived as a 1:1 message of the participant and the answer went
to them in private; it is now dropped in `parse_batch`, and the 1:1 messages of the same delivery
still arrive. A `group_id` that names no group — absent, `null`, empty or blank — is read as a
1:1 message, exactly as before. **Inert for a host until it moves its pin of this library.**

**Evolution fills the group fields when you opt in (since 0.1.9), with `accept_groups=True`.** The
default, `False`, is byte for byte the old drop of every `@g.us`. With it on, a group `message`
arrives with:

| field | value in a group |
|---|---|
| `sender` / `conversation_id` | the ROOM's `key.remoteJid` (`…@g.us`) — the two are equal, so a host can never read a group as a 1:1 |
| `author` | `key.participantAlt` — the member's PHONE JID (`<phone>@s.whatsapp.net`). `key.participant` is always the member's `<lid>@lid`, which does NOT route, so it is never the author |
| `author_is_bot` | `False` (WhatsApp has no `is_bot`) |
| `reply_to_message_id` | `data.contextInfo.stanzaId` — the id of the message this one quotes (a top-level sibling of `message`, null on a plain message), exposed so a host can decide `reply_to_bot` against its own record of what it sent |
| `mentioned_ids` | `data.contextInfo.mentionedJid` — the LIDs (`<digits>@lid`) the message @-mentions, verbatim (since 0.1.10), exposed so a host can decide `mentions_bot` against the bot's own LID |

The author comes ONLY from a phone JID: **a group message with no `key.participantAlt`, or one that
is not a `@s.whatsapp.net` JID, is dropped** (there is no `@lid`→phone bridge to route it). `key.
fromMe` still drops (the bot's own echo). `mentions_bot` is NOT set by the parser: a WhatsApp
@mention names the bot by its own `@lid`, which the pure parser does not know, so it only EXPOSES
the mentioned LIDs in `mentioned_ids` and the HOST decides the mention. **`reply_to_bot` is yours to
decide from `reply_to_message_id`**: the parser is pure and does not know which ids are the bot's own
— a host that keeps a short record of the ids it sent to a room (as the Cogno host does, in its room
buffer) reads a reply to one of them as a call to the bot.

To learn the bot's own LID for `mentions_bot`, call **`await channel.group_participants(group_jid)`**
beside a room's registration (never on a turn): it returns `(owner_jids, members)` — the `ownerJid`
of every `fetchInstances` item whose `name == instance` (a tuple) and the room's `(id, phoneNumber)`
members from `GET /group/findGroupInfos` (one flat object; its `subject`, the room's name, is never
read or logged). Apply «exactly one, or nothing»: with exactly one owner candidate, the bot's LID is the
`id` of the one member whose `phoneNumber` matches it; 0 or more than one owner, or 0 or more than
one matching member, is ambiguous — store nothing. A message whose `mentioned_ids` contains the
stored LID called the bot. Any read failure yields `((), ())` — store no LID and a mention cannot
fire (never a guessed LID). The `fetchInstances` item's `token` and `number` are never read,
returned or logged.

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

**Answering in a group (Telegram, 0.1.4):** send to `msg.sender` (the room) and set
`reply_to_message_id=msg.message_id`. The first text chunk is threaded to that message
(`reply_parameters`, with `allow_sending_without_reply`, so a deleted original never costs the
reply), and the rest go out as before. A value that is not a positive integer is ignored, and
the other adapters ignore the field. A button tapped in a group is dropped at parse, so a group
reply should carry none.

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

**Send retries (write-safe only).** The Evolution and WhatsApp Cloud adapters retry a send a few
times (0.5 s → 1 s → 2 s backoff, 3 attempts total) but ONLY where the request provably never
reached the provider — a connection failure, a 429 (a short `Retry-After` honoured; a long one is
returned as a failure rather than hung on), or a 502/503/504. A **read timeout** is never retried:
it could duplicate a message the provider already accepted, which on a billed/template channel is
the costly harm. Telegram keeps its own, deliberate read-timeout retry — on a conversational
channel a duplicate beats a silently lost reply.

### Markup: send markdown, get the channel's dialect

**Write `**bold**` and stop thinking about it.** Every adapter converts markdown bold into its
own channel's dialect on the way out — `*bold*` on WhatsApp, stripped on Telegram and on web —
via `to_channel_markup`, which is exported if you ever need it directly.

**Tables, on WhatsApp, Telegram and web (0.1.7).** None of them renders a markdown table (the web
widget has no markdown renderer — the reason its bold strips), so a reply that carried one reached
the contact as raw pipes and dashes. Those adapters now send it as a list
(`tables_to_lists`): the header's cells joined by « — » on the first line, then one
`• a — b — c` line per row; the delimiter row and empty cells are dropped, a `**bold**` cell is
still converted. The header is ONE first line rather than a prefix on every row: with three or
four columns a prefix repeats the column names on each line. A table is GFM's — a piped header
row with a delimiter row of as many cells right under it — so a line with a pipe in a sentence,
a block of piped lines without that row, and anything inside a fenced code block are left as
written, and text with no table is the same string. `markdown` and `plain` keep the table; the
day the web widget renders markdown, `web` leaves the set together with its bold cell.

Telegram and web strip because neither renders markup **today**: this gateway sets no Telegram
`parse_mode`, and the web widget draws the bubble as `whitespace-pre-wrap` with no markdown
renderer behind it (measured, not assumed). Both cells carry that condition in the table, so
they can be turned by whoever changes the surface.

This is the gateway's job and not yours for one reason: **it is the only layer that knows the
channel.** A tool has no channel in its call context, and the layer that writes the final reply
(the voicer) rewrites the marker anyway — measured on a live trace, an executor draft carrying
one asterisk was delivered carrying two, with every other byte identical. So a formatting
decision taken anywhere upstream is a suggestion; taken here it is the last word.

It is a delimiter swap, not a markdown renderer: only a paired `**` run is touched, by
CommonMark's own flanking rule, on one line. `2 * 3 * 4`, `2 ** 3 ** 4`, a glob, a bullet and an
unpaired `**` all reach the contact exactly as written. It runs before `split_message`, so a
bold run split across chunks is converted while its pair is still intact, and it is idempotent —
calling it twice changes nothing.

> **What this does NOT promise.** That the contact's app renders the result as bold. That is
> third-party behaviour and can only be established by sending to a real device. What is pinned
> here is that the payload leaves with the channel's documented marker.

#### The conversion leaves a record you can join against

Every adapter that sends emits one INFO line right after converting:

```
channel=whatsapp       event=outbound_markup chars_in=544 chars_out=540
channel=whatsapp_cloud event=outbound_markup chars_in=544 chars_out=540
channel=telegram       event=outbound_markup chars_in=544 chars_out=536
channel=web            event=outbound_markup chars_in=544 chars_out=536
```

It exists because nothing else in the stack can answer "what did the contact actually get?":
you persist the reply *before* handing it to `send`, and the conversion happens *inside* it, so
every stored number is pre-adapter. **`chars_in` is the figure you already log yourself** on the
line above your `channel.send(...)` — join the two and the pre/post pair closes; a mismatch says
something changed the text between your door and the adapter.

Three properties worth knowing before you wire dashboards to it:

- **Lengths, never the text.** The outbound reply is the contact's own data. Nothing here will
  ever carry it, so this line needs no redaction path and no purge path.
- **One line per outbound message, not per chunk** — the conversion runs before `split_message`.
  A 1558-character reply split into three chunks still logs once.
- **Emitted even when nothing changed.** A reply with no bold pair gives `chars_in ==
  chars_out`; that is a real message with no markup in it. The *absence* of the line means the
  conversion did not run — which is the distinction the record exists to draw. (No channel is on
  the identity cell: WhatsApp swaps the marker, Telegram and web strip it.)

It is INFO on purpose, which is a declared exception to this library's own "INFO is not
per-request" rule — see `LOGGING.md`. At DEBUG it would sit behind a switch that is off in
production, and a record nobody can read in production is not a record.

> **On Telegram the host does not decide about the FIRST transport failure — the channel
> already retried it once.** `TelegramChannel` pauses 0.5 s and repeats the one HTTP call that
> failed, and only a second failure becomes the `ok=False` you see. So a `SendResult(ok=True)`
> from this channel can be the second attempt, and **Telegram delivery is at-least-once**: the
> Bot API has no idempotency key, no client-supplied message id and no cheap read-back, so after
> a read timeout "it never arrived" and "it arrived and the answer was lost" are genuinely
> indistinguishable. The trade is deliberate and is written out in `TelegramChannel._post` — a
> duplicate reply is understood in a second, a silently lost one is never noticed, because the
> contact's own message showed as delivered on their side and they have no reason to repeat it.
>
> Two bounds worth knowing as an integrator. The retry is per **HTTP call**, not per `send()`,
> so on a chunked reply the maybe-duplicate is confined to one chunk rather than re-delivering
> every chunk that already succeeded. And it turns on `httpx.TransportError` only — no HTTP
> response ever existed — so no status code is ever retried: a 4xx and a 5xx both mean the
> server answered, and an answer repeated is the same answer.
>
> **The other channels do not do this**, and that asymmetry is not yet expressed in the `Channel`
> port: `cloud`, `evolution` and `web` still surface the first transport failure to you. A host
> that treats every channel as at-most-once is wrong about Telegram; one that assumes
> at-least-once everywhere will under-deliver on the rest. `message_ids` is best-effort for the
> same reason — when a call times out and its retry succeeds, the id recorded is the retry's.

> **`ChannelConfig.timeout` is now the READ budget, not the whole call.** It used to be handed
> to httpx as one number covering connect, read, write and pool alike, so a host that raised it
> to give a slow provider room was also giving a dead address that much time to fail in. The
> connect phase is now separate and short — 5 s by default, `COGNO_HTTP_CONNECT_TIMEOUT` — and
> capped at your value when yours is smaller, so lowering `timeout` still lowers everything.
>
> **And the connection walks address families.** One address per family, IPv6 then IPv4 by
> default, falling through on a dead connect *or* a dead TLS handshake — the second one is the
> case the stack below does not handle, because anyio's RFC 6555 race ends when the TCP connect
> wins and the handshake runs on that winner alone. `COGNO_HTTP_IP_FAMILY=auto|ipv4|ipv6`
> (default `auto`) if your network wants the decision made for it; an unrecognised value falls
> back to `auto` rather than failing a send.
>
> Two things this changes for you as an integrator. The worst case for an unreachable provider
> is now *families × connect* (2 × 5 s) instead of one full `timeout`, and on Telegram the retry
> above it makes that 4 × 5 s before you see `ok=False` — bounded, and no longer multiplying a
> dead family's wait. And when nothing connects, `SendResult.error` reads
> `ip_family_exhausted (ipv6: ConnectTimeout; ipv4: ConnectError)`: the families that were tried
> are named, because an error that does not say which one failed makes the next person guess.
> The host is deliberately absent from that string — an Evolution `base_url` is a tenant's own
> instance.

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
