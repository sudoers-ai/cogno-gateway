# Changelog

## Unreleased

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
