# Changelog

## 0.1.10 — 2026-11-13

### Added
- **The Evolution adapter EXPOSES the LIDs a WhatsApp group message @-mentions, for a host to
  decide the mention itself.** A new reserved, keyword-only `InboundMessage.mentioned_ids`
  (default `()`, additive and hidden from `repr`/the 1:1 digests at its default, exactly like
  `reply_to_message_id`): with `accept_groups=True`, a group message fills it from
  `data.contextInfo.mentionedJid`, verbatim (`<digits>@lid`). The parser stays PURE and never sets
  `mentions_bot` — a WhatsApp mention is by the bot's own LID, which the library does not know — so
  the HOST matches `mentioned_ids` against the room's stored `bot_lid`.
- **A new `EvolutionChannel.group_participants(group_jid)` read** — the two network reads a host
  asks BESIDE a room's registration (never on a turn) to learn the bot's own LID: `GET
  /instance/fetchInstances?instanceName=<instance>` (a LIST of flat items; the `ownerJid` of every
  item whose `name == self.instance`, as a tuple) and `GET /group/findGroupInfos/{instance}?groupJid=
  <group>` (one flat object; the `(id, phoneNumber)` members — its `subject`, the room's third-party
  name, is never read or logged). It returns `(owner_jids, members)` for the host's
  pure matcher, which applies «exactly one, or nothing» (the gateway surfaces the raw candidates so
  the host can tell a 0-match from an ambiguous >1 — never the first of several). Any failure yields
  `((), ())` (the host then stores no `bot_lid` and a mention cannot fire). The item's `token` (a
  secret) and `number` are never read, returned or logged; the one log line
  (`event=group_participants_failed`) carries only the exception class.

## 0.1.9 — 2026-11-12

### Added
- **The Evolution adapter parses a WhatsApp GROUP message when you opt in (`accept_groups=True`),
  off by default (byte for byte the old drop of every `@g.us`).** With it on, a group `message`
  fills the group fields of `InboundMessage`: `conversation_id` and `sender` are the room
  (`key.remoteJid`, always equal, so a host can never read a group as a 1:1), and `author` is the
  member's PHONE JID taken from `key.participantAlt` (`<phone>@s.whatsapp.net`). The author comes
  ONLY from a phone JID — `key.participant` is always the member's `<lid>@lid`, which does not
  route and is never promoted, so a group message with **no `participantAlt`, an `@lid`, or any
  other host is dropped** (there is no `@lid`→phone bridge to route it). `key.fromMe` still drops
  the bot's own echo. `author_is_bot` is always `False` (WhatsApp has no `is_bot`).
- **A new reserved, keyword-only `InboundMessage.reply_to_message_id`** (default `""`, additive and
  hidden from `repr` at its default, so a 1:1 message is byte for byte as before). The Evolution
  adapter fills it in a group with `data.contextInfo.stanzaId` — the id of the message this one
  QUOTES (a top-level sibling of `message`, null on a plain message, read independently of
  `messageType`). It is exposed so a host can decide `reply_to_bot` itself, against its own record
  of the ids it sent to the room; the parser stays pure and never decides it. `mentions_bot` is
  deliberately NOT set by the Evolution group parser: a WhatsApp @mention names the bot by its own
  `@lid`, which this library does not yet carry.

## 0.1.8 — 2026-11-11

### Changed

- **BREAKING (default): provider webhooks now require a secret by default.**
  `ChannelConfig.require_secret` now defaults to **`None` (unset)**, which means *the kind's
  secure default*: a Telegram / Evolution / WhatsApp Cloud webhook **fails closed** (rejects an
  inbound delivery when no webhook secret is configured — anyone who learns the URL could
  otherwise forge one), while the **web widget stays open** (it is the host's own
  auth/CORS-guarded chat route, with no third party to share a secret with, so failing it closed
  would break the web chat for no gain). An **explicit `True`/`False` is honoured by every kind**:
  a deployment that genuinely has no secret opts out with `require_secret=False`, and a host that
  wants the web widget verified sets `require_secret=True` on it. Read the posture through
  `ChannelConfig.secret_required(default=...)`, never by truthiness. This first shipped
  open-by-default in 0.x; provider webhooks are now secure by default. The refusal log line says
  how to opt out and never logs the secret.

### Added
- **Write-safe send retries for the Evolution and WhatsApp Cloud channels.** A send is now
  retried on a failure where nothing provably reached the provider — a connection failure, a
  `429` (honouring a short `Retry-After`; a value over 5 s is returned as a failure rather than
  waited out, since the send runs inside a turn), or a `502`/`503`/`504` — with a growing
  backoff (0.5 s → 1 s → 2 s, 3 attempts total). A **read timeout is never retried**: it could
  duplicate a message the provider already accepted, which on a per-message/template channel is
  the costly harm. Telegram's existing, deliberate read-timeout retry is unchanged (on a chat
  channel a duplicate beats a silently lost reply). Each retry logs the channel, the error class
  and the attempt number — never the message text or the recipient.

## 0.1.7 — 2026-11-04

### Fixed
- **A markdown table sent to WhatsApp, Telegram or the web widget showed as raw pipes.** None of
  them renders a table (the web widget has no markdown renderer, the same reason its bold cell
  strips), and an instruction to the model not to write one is not a guarantee, so the conversion
  is here, beside the bold dialect, where the channel is known. On those three channels
  `to_channel_markup` now turns every table into a list (`tables_to_lists`, exported): the
  header's cells joined by « — » on the first line, then one `• a — b — c` line per row; the
  `|---|` delimiter row and empty cells are dropped, and a `**bold**` cell is still converted.
  - A table is GFM's: a header row with a pipe, the delimiter row right under it with as many
    cells (`---`, `:--:`, `--:`), then the piped rows. NOT a table, and left byte for byte: a
    line with a pipe in a sentence, piped lines with no delimiter row (dashes in a cell are not
    one), a delimiter of another width, and anything inside a fenced code block.
  - Text with no table is the same string; converting twice equals converting once.
  - `markdown`, `plain` and an unknown channel keep the table. The day the web widget renders
    markdown, `web` leaves the set together with its bold cell.
  - `tests/unit/test_markup_tables.py`, through the three WhatsApp/Telegram adapters to the wire
    and through web's `serialize`.

## 0.1.6 — 2026-11-03

### Added
- **THIS bot joined or left a Telegram group, as one event** (`MessageKind.MEMBERSHIP`, with the
  same `ChannelConfig.accept_groups` switch on). A host that wants to register a room when the bot
  is put in it had nothing to read: `my_chat_member` was not subscribed, and the service message
  that announces the bot arrived as a group message of kind `UNKNOWN`. Measured on `df5b9a0`: a
  `my_chat_member` naming the bot parsed to `None`, and a `new_chat_members` or a
  `left_chat_member` naming it to kind `unknown`.
  - `InboundMessage.membership` (keyword-only, default `None`, out of `repr` at its default like
    the five group fields before it) holds a `Membership(status, title)`. `status` is
    `MEMBERSHIP_JOINED` or `MEMBERSHIP_LEFT`, the provider's vocabulary folded at parse; `title`
    is the room's name. `sender` and `conversation_id` are the room; `author` is empty, because
    who added the bot is not the host's to keep.
  - From `my_chat_member`: `member`, `administrator`, `restricted` with `is_member` → joined;
    `left`, `kicked`, `restricted` without it → left. It is the only shape that reports a removal.
  - From the service messages: `new_chat_members` naming the bot, `group_chat_created` and
    `migrate_from_chat_id` (the new room of a group upgraded to a supergroup) → joined;
    `left_chat_member` naming the bot and `migrate_to_chat_id` (the old room) → left.
  - Dropped: the switch off, a private chat or a channel, a forum, a member that is not this bot
    (by id, or by handle on a user marked `is_bot`). A service message about somebody else parses
    as before.
- **Does the bot read every group message? (BotFather's privacy mode.)**
  `TelegramWebhookRegistrar.reads_all_group_messages(token=…)` and
  `TelegramChannel.reads_all_group_messages()` read `getMe`'s `can_read_all_group_messages`:
  `False` = privacy mode ON, `True` = off, `None` = UNKNOWN (no token, a failed call, the field
  absent or not a JSON boolean) — never a guess. `NullTelegramRegistrar` answers `None`. With
  privacy ON, Telegram delivers to a bot in a group only the commands addressed to it, the replies
  to its messages and the service messages: an @mention alone never arrives, so a host serving
  mentions needs to say so. One parser, `telegram_provisioning.reads_all_of`. The channel's read is
  one `GET`, no retry, a 3 s timeout; a failure logs the class only.
- **`_ALLOWED_UPDATES` gains `my_chat_member`.** A bot registered before 0.1.6 keeps its old
  subscription until `TelegramWebhookRegistrar.register` runs again; until then it still sees a
  join (the service message) and not a removal.

### Unchanged
- The 1:1 parse of every adapter: the corpus digests of `tests/unit/test_inbound_group_fields.py`
  hold. Its `asdict` pin lists the new reserved key at its default, and the Cloud controls compare
  every other key byte for byte.
- No new log line.

### Tests
- `tests/unit/test_telegram_membership.py`: each shape both ways, the controls (another member, a
  person named like the bot, a service message about somebody else), the drops, and the switch.
  10 mutations, each killed. `tests/unit/test_telegram_provisioning.py`: the privacy read, both
  answers, five kinds of unknown, no call without a token, the null. `tests/integration`: the
  registrar subscribes and the update parses; the channel's privacy read over real httpx.

## 0.1.5 — 2026-11-02

### Added
- **`probe(account)` on `EvolutionWhatsAppProvisioner`, `InMemoryWhatsAppProvisioner` and the
  `WhatsAppProvisioner` Protocol: the status, READ-ONLY.** `status()` heals a stale return address
  with `POST /webhook/set/{instance}`, a write on the provider, so a caller answering an HTTP `GET`
  (a dashboard polls one) had no way to ask without writing. `probe()` makes the same two reads
  (`connectionState`, `webhook/find`) and the same comparison (URL, and `headers.apikey` when a
  secret is set), logs `event=webhook_stale` the same way, and stops there: it does not ask
  whether the public URL is alive, because that question only gates a write. The stub has nothing
  to heal, so its `probe()` is its `status()`.

### Unchanged
- `status()` answers exactly as in 0.1.4. The two share one private reading (`_inspect`), so they
  cannot disagree about what counts as stale: wherever `status()` does not write (the address is
  right, there is no `webhook_base`, the probe failed, the public URL is dead) the two return
  equal values, pinned in `tests/unit/test_provisioning.py`.

## 0.1.4 — 2026-11-02

### Added
- **Telegram group chats, opt-in: `ChannelConfig.accept_groups` (default `False`) and
  `ChannelConfig.bot_username`.** With the switch off, every group shape drops at parse exactly as
  in 0.1.3, unconditionally. On, a `group`/`supergroup` `message` parses with the five group fields
  of `InboundMessage` filled:
  - `sender` and `conversation_id` are the ROOM's `chat.id`, and `author` is the member's
    `from.id`. `sender` stays the room on purpose: Telegram numbers messages PER CHAT, so a host
    that deduplicates on `(sender, message_id)` would otherwise drop the same member's message 57
    in a second room as a duplicate of the first. It also keeps a reply sent to `sender` in the room;
  - `author_is_bot` is `from.is_bot`. Telegram can deliver another bot's messages to a bot whose
    privacy mode is off or that is an admin ("Bot-to-Bot Communication",
    https://core.telegram.org/bots/features, read 2026-11-02);
  - `mentions_bot` is read from `entities` (or `caption_entities` on media). A `mention` counts
    when its text is `@<bot_username>` (any case, with or without the configured `@`), a
    `text_mention` when its user is this bot, and a `bot_command` when it is addressed
    `/<cmd>@<bot_username>`. **Offsets and lengths are UTF-16 code units**, the Bot API's unit, so a
    mention after an emoji outside the BMP is found where a code-point slice would miss it. The text
    alone is never read: the handle inside a URL carries no entity and is no mention. A malformed
    entity is skipped, never raised, and a `bool` is not an offset. No `bot_username` → never a mention;
  - `reply_to_bot` is true when `reply_to_message.from` is this bot: by the id in the token's
    prefix (`<bot id>:<secret>`), or, for a token without one, by the handle on a user Telegram
    marks `is_bot`, so a person whose username equals the handle is not the bot.
- **Still dropped with the switch on:**
  - a group button tap or reaction (Phase 1 sends no buttons to a room);
  - a message posted on behalf of a chat (`sender_chat`: an anonymous admin, a linked channel's post);
  - a message with no `from`;
  - a forum supergroup (`is_forum`), whose topics a reply would have to address.
- **`OutboundMessage.reply_to_message_id`** (default `""`). Telegram threads the FIRST text chunk
  to it (`reply_parameters`, with `allow_sending_without_reply`), so a room sees whom the bot
  answers. A value that is not a positive integer sends exactly as before, and the other adapters
  ignore the field.

### Unchanged
- Evolution (`@g.us`) and WhatsApp Cloud (`group_id`) still drop every group message. The 1:1
  parse of every adapter is the same with the switch on or off: same `repr` (the twins in
  `tests/unit/test_telegram_group_parse.py`, beside the 1:1 corpus digests of
  `tests/unit/test_inbound_group_fields.py`, taken on `3ff003b`, which still hold).
- No new log line.

### Not in this release
- Which rooms may reach a turn, which trigger counts and who may speak are the HOST's decisions;
  this library only parses. A host moves its pin to read the fields.
- `my_chat_member`, which would let a host discover the rooms the bot is put in, is not added to
  `_ALLOWED_UPDATES`, so no bot has to re-register its webhook.

## 0.1.3 — 2026-10-23

### Fixed
- **A failed Telegram send no longer reports the bot token, in the log or in `SendResult.error`.**
  The token is in the path of every Bot API URL (`/bot<TOKEN>/sendMessage`), and httpx writes the
  request URL into the text of the `HTTPStatusError` that `raise_for_status` raises. For an answer
  with no body (a redirect, a proxy's 5xx) `_error_detail` fell back to that text, and the
  `event=send_failed` WARNING line logged it. Measured on 0.1.2 with a planted token over real
  httpx (`MockTransport`): a 502 and a 302 with no body, and a 500 whose HTML echoed the URL, each
  put the token into `SendResult.error` and into the log; Telegram's JSON description did not.
  - `event=send_failed` now reads `sent=<n> error=<ExceptionClass> status=<code|none>` and carries
    nothing the exception or the provider wrote. `event=send_retry` reads `after=<ExceptionClass>`.
  - `SendResult.error` for an answer with no body is `<status> <ExceptionClass>`
    (`502 HTTPStatusError`), never the exception's text.
  - Every text that does reach `SendResult.error` (Telegram's description, a non-JSON body, a
    transport error's message) has the token masked as `[token]`, as written and percent-encoded
    in either case, so `/bot<TOKEN>/` reads `/bot[token]/`. An error text with no token in it is
    kept byte for byte, including a description that says "bot".
  - `tests/unit/test_telegram_send_error_carries_no_token.py` is the sentinel: a planted token over
    real httpx, and no 6-character window of it in any record the package logs or in
    `SendResult.error`, across seven failing shapes and all four Bot API calls a send makes, each
    proving first that the token was in the failing request's URL. Its controls keep a description,
    a description naming the bot, a plain-text body and a read timeout whole.

### Not in this release
- `fetch_media` still lets the `HTTPStatusError` of `getFile` or of the download propagate, and its
  text carries the URL with the token. Changing what it raises changes what a host catches, so it
  is left to its own change; a host that logs that exception's text logs the token.
- httpx's own `httpx` logger writes every request URL, token included, at INFO. That logger is the
  host's to configure (`LOGGING.md`).

## 0.1.2 — 2026-10-21

### Fixed
- **A WhatsApp Cloud reply in a group is dropped at parse, like every other adapter's group
  traffic.** Meta delivers a reply in a group the business created through its Groups API under
  the same `messages` webhook field as a 1:1 message, and marks it only by a `group_id` inside the
  message object, with `from` set to the participant ("The `message` object includes a `group_id`
  field to indicate this is a group message",
  https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/groups-messaging/,
  read 2026-10-21). `WhatsAppCloudChannel` did not read it, so the reply ran a turn as the
  participant and the answer went to them in private. `WhatsAppCloudChannel._is_group` now drops
  it in `parse_batch` (and so in `parse_inbound`), for every message kind, without dropping the
  1:1 messages delivered beside it and without logging it as a parse failure. The drop is still
  unconditional: no group field is filled and nothing of a group reaches a host.
- **A `group_id` that names no group stays a 1:1 message** — absent, JSON `null`, empty or blank.
  Meta's page says nothing of an empty one; an empty value is no group a reply could be addressed
  to, the adapter already reads a present-but-null field as absent, and dropping a real 1:1
  message on a blank key would lose it with nobody told. Those four shapes parse byte for byte as
  on `6e7dc71` (`repr` and `asdict` digests taken there), and the 26-message 1:1 corpus is
  unchanged. The strict `xfail` `test_a_cloud_group_reply_is_not_dropped_yet` is gone: the Cloud
  shapes (text, interactive, template button, reaction, location, image, unknown) joined the drop
  table of `tests/unit/test_inbound_group_fields.py`, each beside its twin without the `group_id`.

## 0.1.1 — 2026-10-21

### Added
- **`InboundMessage` reserves the fields a group chat needs — and nothing reads or fills them
  yet.** A group separates WHERE a reply goes from WHO spoke; the model had only `sender` for
  both. Five keyword-only fields, each at the value that IS the 1:1 case: `conversation_id`
  (`""` = reply to `sender`), `author` (`""` = the sender wrote it), `mentions_bot`,
  `reply_to_bot`, `author_is_bot` (`False`). **Group traffic is still dropped at parse,
  unconditionally** — Telegram `group`/`supergroup` (message, button tap, reaction) and Evolution
  `@g.us` (message, presence) — so a host still only receives 1:1 messages, and for those the
  change is invisible: `tests/unit/test_inbound_group_fields.py` parses a fixed 1:1 corpus
  through all four adapters (26 messages) and pins `repr` byte for byte against digests taken on
  `3ff003b`; `__match_args__`, positional construction and the order of the old fields are the
  old ones. The fields stay out of `repr` while at their default (`InboundMessage.__repr__`).
  `dataclasses.asdict` is the one place they appear, and it is pinned exactly: the five keys at
  their default, every other key byte for byte the old one. `dataclasses.replace` carries them
  through (a host normalises the WhatsApp `sender` that way).
- **Known gap, written down instead of pinned as correct:** WhatsApp Cloud delivers a reply in a
  group the business created through Meta's Groups API under the same `messages` webhook field,
  with a `group_id` inside the message object (Meta's Groups API docs, read 2026-10-21), and
  `WhatsAppCloudChannel` does not read it — such a reply is parsed as a 1:1 message of the
  participant. `test_a_cloud_group_reply_is_not_dropped_yet` is a strict `xfail` describing the
  drop; it turns into a failure the day the parser is fixed, so the fix has to flip it.

- **The Telegram twin of the WhatsApp provisioner** — `cogno_gateway.telegram_provisioning`
  (`TelegramWebhookRegistrar`, `NullTelegramRegistrar`, `build_telegram_registrar`), exported at
  the package root. The library declared this hole by omission: `TelegramChannel` **verifies**
  `X-Telegram-Bot-Api-Secret-Token` on every delivery, fail-closed in production, and nothing
  here ever told Telegram to **send** one. Verification without registration is half a lifecycle
  — an application pairing a bot from its own admin surface got a channel born with no webhook
  and no secret, the only writer in existence was an operator shell script, and "which features
  work" therefore depended on which writer ran last. `register` (`setWebhook`), `unregister`
  (`deleteWebhook`, with the 24-hour backlog as an explicit `drop_pending` choice rather than a
  default nobody sees), and `bot_username` (`getMe`, best-effort: a label must never fail an
  activation that worked). Errors come back as `(False, description)` in Telegram's own wording.
  Moved in from an application that had written it alone; a webhook REGISTRAR belongs beside the
  channel that VERIFIES what the registration causes to be sent, and the `allowed_updates` list
  is now one definition instead of two that drift.

- **The markup conversion now leaves a record** — `channel=… event=outbound_markup
  chars_in=… chars_out=…`, at INFO, from every adapter that sends. It exists because the
  question "does the `**` still reach the contact?" had no column anywhere that could answer
  it: the host persists the reply before the adapter sees it, its own `outbound_attempted
  chars=` is measured on the line above `channel.send(...)`, and the conversion happens inside
  `send`. **Lengths only, never the text** — the outbound reply is the contact's own data, and
  a log that carried it would open a store of personal data to close a hole in observability.
  One line per outbound message, not per chunk.

### Fixed
- **The outbound connection falls back across address families, on BOTH halves of the bind.**
  A reply did not leave the box because the connection bound to an IPv6 address that completed
  the TCP handshake and then never completed the TLS one; it was contained by hand with a line in
  `/etc/hosts`, which is not code. The stack below does less than it looks like: anyio races the
  TCP connect across families (RFC 6555), but `httpcore` runs `start_tls` on the single stream
  that won that race, with no address left to go back for — and the address that answers TCP
  instantly is the one that wins. `cogno_gateway.net` now walks the families itself (one address
  per family, IPv6 then IPv4, `COGNO_HTTP_IP_FAMILY=auto|ipv4|ipv6`), falling through on a dead
  connect *or* a dead handshake. SNI and certificate verification still name the provider — the
  address is chosen below the origin, never by rewriting the URL. When nothing connects, the
  error names the families: `ip_family_exhausted (ipv6: ConnectTimeout; ipv4: ConnectError)`.
- **Connect and read are separate timeouts.** `ChannelConfig.timeout` was handed to httpx as one
  number covering connect, read, write and pool alike, so an address that never answers could
  spend the budget meant for a provider taking its time. Connect is now short (5 s,
  `COGNO_HTTP_CONNECT_TIMEOUT`, never longer than the read budget); `ChannelConfig.timeout` is
  the read budget it always described. Every adapter builds its client through the one
  constructor `build_async_client`, pinned by a guard derived from the package.

### Changed
- **The version is now `0.1.1`** (`pyproject.toml`). This heading was `Unreleased`; every entry
  under it — the ones above and below, unreleased since `0.1.0` — ships with this number.
- `httpx>=0.27` and an explicit `httpcore>=1.0`: the family fallback is installed as an
  `httpcore` network backend, and 0.27 is the first httpx line with the 1.x pool shape it needs.

- **Outbound markup is converted per channel.** WhatsApp bold is a SINGLE asterisk; the
  markdown a voicer writes is a double one, and nothing converted between them, so replies
  reached the contact reading `**Setembro de 2026**`. Every adapter now converts on the way
  into the provider payload (`to_channel_markup`, exported): `*bold*` on WhatsApp, stripped on
  Telegram (this gateway sets no `parse_mode`) and on web (the widget renders
  `whitespace-pre-wrap` with no markdown renderer). A delimiter swap by
  CommonMark's flanking rule, not a markdown renderer — `2 * 3 * 4`, globs and unpaired `**`
  are left exactly as written — applied before chunking and idempotent.

## 0.1.0 — 2026-07-25

First public release on PyPI.

Messaging transport edge for the Cogno cognitive pipeline — Telegram / WhatsApp (Evolution) / Web channel adapters behind a normalized, content-typed message model
