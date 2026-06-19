"""
Deterministic "data → buttons" pattern (no LLM, no anima/gateway changes).

When a turn produced a list of choices (a tool returned the available services /
slots), the host maps that data to buttons (≤3) or a list menu (>3); the user's
tap comes back as an INTERACTIVE selection that feeds the next turn.

Standalone (fake data, no network):  python examples/host_choices.py
"""

from __future__ import annotations

from cogno_gateway import (
    ButtonReply,
    InboundMessage,
    MessageKind,
    WebChannel,
    options_to_message,
    user_input,
)


def extract_choice_list(ego_result) -> list[dict]:
    """Host-specific: scan the EGO trace for a tool output shaped like a choice
    list (``[{id, name}, …]``). Here we fake the EGO result with a plain dict."""
    for step in ego_result.get("steps", []):
        out = step.get("output")
        if isinstance(out, list) and out and all("id" in x and "name" in x for x in out):
            return out
    return []


def main() -> None:
    web = WebChannel()

    # ── Turn 1: the EGO called list_services(); the SUPEREGO voiced a prompt ──
    fake_ego_result = {"steps": [{"output": [
        {"id": "corte", "name": "Corte"}, {"id": "barba", "name": "Barba"},
        {"id": "mani", "name": "Manicure"}, {"id": "massa", "name": "Massagem"}]}]}
    reply_text = "Qual serviço você quer agendar?"

    options = extract_choice_list(fake_ego_result)
    out = options_to_message(reply_text, options, list_button="Ver serviços")
    print("outbound →", web.serialize("sess-1", out))
    print(f"  ({len(options)} options → "
          f"{'buttons' if out.buttons else 'list menu'})")

    # ── Turn 2: the user taps an option → INTERACTIVE inbound ────────────────
    tapped = InboundMessage(channel="web", sender="sess-1", kind=MessageKind.INTERACTIVE,
                            selection=ButtonReply(id="corte", title="Corte"))
    print("\nuser tapped → pipeline input:", repr(user_input(tapped)))
    # → feed "corte" into the next pipeline turn as the user's choice.


if __name__ == "__main__":
    main()
