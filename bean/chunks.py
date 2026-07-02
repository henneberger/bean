"""Chunking: fixed line windows with overlap, capped per chunk. Boring on purpose — stable
chunk ids (doc_id#L<start>) mean an unchanged document re-embeds nothing."""

from __future__ import annotations

from dataclasses import dataclass

CHUNK_LINES = 40
CHUNK_OVERLAP = 8
MAX_CHARS = 2000
MIN_CHARS = 40


@dataclass
class Chunk:
    id: str
    start: int  # 1-based line
    end: int
    text: str


def chunk_text(text: str, doc_key: str) -> list[Chunk]:
    lines = text.split("\n")
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    out: list[Chunk] = []
    for start in range(0, len(lines), step):
        window = lines[start:start + CHUNK_LINES]
        body = "\n".join(window).strip()
        if len(body) >= MIN_CHARS:
            out.append(Chunk(
                id=f"{doc_key}#L{start + 1}",
                start=start + 1,
                end=min(start + CHUNK_LINES, len(lines)),
                text=body[:MAX_CHARS],
            ))
        if start + CHUNK_LINES >= len(lines):
            break
    return out
