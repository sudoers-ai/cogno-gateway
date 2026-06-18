"""
Minimal host wiring for cogno-gateway: a webhook payload → normalized message →
reply → send.

The Web channel runs standalone (no network):  python examples/host_min.py
Telegram/WhatsApp are shown as wiring (they'd hit the provider API).

The host's job is the glue the gateway deliberately leaves out:
  verify → parse_inbound → [audio? fetch_media → vox STT] → PIPELINE → reply
         → [vox TTS] → send
"""

from __future__ import annotations

import asyncio

from cogno_gateway import (
    ChannelConfig,
    MessageKind,
    OutboundMessage,
    WebChannel,
    create_channel,
)


async def main() -> None:
    # ── Web channel (the cogno-cloud-ui chat contract) — no provider call ──
    web = WebChannel()
    inbound = web.parse_inbound({"session_id": "sess-1", "message": "qual meu saldo?"})
    assert inbound is not None
    print(f"inbound : channel={inbound.channel} sender={inbound.sender} "
          f"kind={inbound.kind.value} text={inbound.text!r}")

    # The host would now run the cognitive pipeline; here we fake the reply.
    reply = OutboundMessage(text="Seu saldo é R$ 1.234,56.")

    # Web 'send' just serializes the dict the host returns in the HTTP response:
    print("outbound:", web.serialize(inbound.sender, reply))

    # ── Other channels: build via the factory with per-tenant config ──────
    create_channel("telegram", ChannelConfig(token="<BOT_TOKEN>", secret="<WEBHOOK_SECRET>"))
    create_channel("whatsapp", ChannelConfig(
        base_url="http://localhost:8080", token="<EVOLUTION_APIKEY>", instance="tenant1"))
    print("\ntelegram/whatsapp channels built (would call the provider API on send).")

    # Rich content is first-class: a reaction inbound, an audio reply, etc.
    react = web.parse_inbound({"session_id": "sess-1",
                               "reaction": {"emoji": "👍", "target_message_id": "m42"}})
    print(f"\nreaction: kind={react.kind.value} emoji={react.reaction.emoji} "
          f"-> msg {react.reaction.target_message_id}")
    voice_reply = OutboundMessage(text="(transcrição)", audio=b"<opus bytes from vox TTS>")
    print(f"voice reply carries {len(voice_reply.audio)} audio bytes "
          f"(MessageKind.AUDIO = {MessageKind.AUDIO.value})")


if __name__ == "__main__":
    asyncio.run(main())
