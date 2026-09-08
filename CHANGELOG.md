# Changelog

## Unreleased

### Added
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
