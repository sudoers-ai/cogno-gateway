"""Unit tests for the data → buttons/list host helper."""

from cogno_gateway import (
    Button,
    InboundMessage,
    ButtonReply,
    MessageKind,
    options_to_message,
    user_input,
)


def test_no_options_is_plain_text():
    out = options_to_message("oi", [])
    assert out.text == "oi" and not out.buttons and out.list_menu is None


def test_few_options_become_buttons():
    out = options_to_message("Confirma?", [("yes", "Sim"), ("no", "Não")])
    assert out.list_menu is None
    assert [(b.id, b.title) for b in out.buttons] == [("yes", "Sim"), ("no", "Não")]


def test_many_options_become_list():
    opts = [(f"o{i}", f"Opção {i}") for i in range(5)]
    out = options_to_message("Escolha:", opts, list_button="Ver")
    assert not out.buttons
    assert out.list_menu.button == "Ver"
    rows = out.list_menu.sections[0].rows
    assert len(rows) == 5 and rows[0].id == "o0"


def test_accepts_dicts_from_tool_results():
    # a typical tool output shape: {id, name}
    out = options_to_message("?", [{"id": "corte", "name": "Corte"},
                                   {"value": "barba", "title": "Barba"}])
    assert [(b.id, b.title) for b in out.buttons] == [("corte", "Corte"), ("barba", "Barba")]


def test_accepts_button_objects():
    out = options_to_message("?", [Button("a", "A")])
    assert out.buttons[0].title == "A"


def test_max_buttons_threshold_configurable():
    opts = [("a", "A"), ("b", "B")]
    out = options_to_message("?", opts, max_buttons=1)
    assert not out.buttons and out.list_menu is not None    # 2 > max_buttons=1 → list


def test_user_input_reads_selection_then_text():
    tapped = InboundMessage(channel="web", sender="s", kind=MessageKind.INTERACTIVE,
                            selection=ButtonReply(id="corte", title="Corte"))
    typed = InboundMessage(channel="web", sender="s", kind=MessageKind.TEXT, text="oi")
    assert user_input(tapped) == "corte"
    assert user_input(typed) == "oi"
