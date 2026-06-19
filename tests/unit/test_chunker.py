"""Unit tests for split_message (ported chunker)."""

from cogno_gateway import split_message


def test_short_text_single_chunk():
    assert split_message("hello") == ["hello"]


def test_empty_text():
    assert split_message("") == []
    assert split_message("   ") == []


def test_splits_paragraphs_within_limit():
    text = "a" * 50 + "\n\n" + "b" * 50
    chunks = split_message(text, max_chars=60)
    assert len(chunks) == 2
    assert all(len(c) <= 60 for c in chunks)


def test_every_chunk_within_max_chars():
    text = " ".join(f"word{i}" for i in range(300))
    chunks = split_message(text, max_chars=80, max_chunks=20)
    assert all(len(c) <= 80 for c in chunks)


def test_caps_at_max_chunks():
    text = "\n\n".join("p" * 40 for _ in range(20))
    chunks = split_message(text, max_chars=50, max_chunks=3)
    assert len(chunks) <= 3


def test_hard_split_long_unbroken_word():
    chunks = split_message("x" * 500, max_chars=100, max_chunks=10)
    assert all(len(c) <= 100 for c in chunks)


def test_splits_long_paragraph_by_sentences():
    # one paragraph far over the limit → falls back to sentence splitting
    para = " ".join(f"Frase número {i} aqui." for i in range(40))
    chunks = split_message(para, max_chars=80, max_chunks=20)
    assert len(chunks) > 1 and all(len(c) <= 80 for c in chunks)


def test_sentence_longer_than_limit_is_hard_split():
    para = "curta. " + "y" * 300 + ". fim."
    chunks = split_message(para, max_chars=100, max_chunks=20)
    assert all(len(c) <= 100 for c in chunks)


def test_overflow_truncated_into_last_chunk_with_ellipsis():
    # many short paragraphs beyond max_chunks, overflow too big to merge → truncated
    text = "\n\n".join(f"Parágrafo {i} com algum texto de tamanho médio aqui." for i in range(30))
    chunks = split_message(text, max_chars=70, max_chunks=2)
    assert len(chunks) == 2
    assert chunks[-1].endswith("(…)") and all(len(c) <= 70 for c in chunks)
