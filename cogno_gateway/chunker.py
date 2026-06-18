"""
cogno_gateway.chunker — split a long reply into messaging-friendly chunks.

Ported clean-room from the parent ``cogno.gateways.chunker.split_message``: group
by paragraphs, fall back to sentences, then hard-split at word boundaries, capped
at ``max_chunks`` (the overflow is merged/truncated into the last chunk). Pure
word math — no env reads (the parent's ``COGNO_MAX_MSG_CHARS`` default becomes the
explicit ``max_chars`` argument).

(The TTS-segment splitter ``split_text_for_tts`` lives in cogno-vox.)
"""

from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 600
DEFAULT_MAX_CHUNKS = 6


def split_message(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[str]:
    """Split ``text`` into 1..``max_chunks`` strings, each ≤ ``max_chars``."""
    if not text or not text.strip():
        return []
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n\n+", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_by_sentences(para, max_chars))
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current.strip():
                chunks.append(current.strip())
            current = para

    if current.strip():
        chunks.append(current.strip())

    # Cap at max_chunks — merge/truncate the overflow into the last chunk.
    if len(chunks) > max_chunks:
        kept = chunks[: max_chunks - 1]
        overflow = "\n\n".join(chunks[max_chunks - 1:])
        if len(overflow) <= max_chars:
            kept.append(overflow)
        else:
            kept.append(_truncate_at_sentence(overflow, max_chars - 4) + " (…)")
        chunks = kept

    # Safety: no chunk may exceed max_chars.
    safe: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            safe.append(chunk)
        else:
            safe.extend(_hard_split(chunk, max_chars))

    if len(safe) > max_chunks:
        final = safe[: max_chunks - 1]
        last = safe[max_chunks - 1]
        if len(last) > max_chars - 4:
            last = _truncate_at_sentence(last, max_chars - 4) + " (…)"
        final.append(last)
        safe = final

    return safe


def _split_by_sentences(text: str, max_chars: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current.strip():
                chunks.append(current.strip())
            if len(sentence) > max_chars:
                chunks.extend(_hard_split(sentence, max_chars))
                current = ""
            else:
                current = sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    best = max(truncated.rfind(". "), truncated.rfind("! "),
               truncated.rfind("? "), truncated.rfind("\n"))
    if best > max_chars // 2:
        return truncated[: best + 1].strip()
    last_space = truncated.rfind(" ")
    if last_space > max_chars // 2:
        return truncated[:last_space].strip()
    return truncated.strip()


def _hard_split(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    while len(text) > max_chars:
        split_at = text[:max_chars].rfind(" ")
        if split_at <= 0:
            split_at = max_chars
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks
