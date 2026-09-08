# Changelog

## Unreleased

### Added
- **The markup conversion now leaves a record** — `channel=… event=outbound_markup
  chars_in=… chars_out=…`, at INFO, from every adapter that sends. It exists because the
  question "does the `**` still reach the contact?" had no column anywhere that could answer
  it: the host persists the reply before the adapter sees it, its own `outbound_attempted
  chars=` is measured on the line above `channel.send(...)`, and the conversion happens inside
  `send`. **Lengths only, never the text** — the outbound reply is the contact's own data, and
  a log that carried it would open a store of personal data to close a hole in observability.
  One line per outbound message, not per chunk.

### Fixed
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
