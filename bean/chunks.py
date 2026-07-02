"""Chunking: fixed line windows with overlap, capped per chunk. Boring on purpose — stable
chunk ids (doc_key#L<start>) mean an unchanged document re-embeds nothing. The window height,
overlap, and size caps all come from config (`chunking.*`) so they can be tuned per workspace;
changing them, like changing the embedding model, is a `bean reembed`."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULTS = {"lines": 40, "overlap": 8, "max_chars": 2000, "min_chars": 40}


@dataclass
class Chunk:
    id: str
    start: int  # 1-based line
    end: int
    text: str


def chunk_text(text: str, doc_key: str, cfg: dict | None = None) -> list[Chunk]:
    c = {**DEFAULTS, **(cfg or {})}
    lines = text.split("\n")
    step = max(1, c["lines"] - c["overlap"])
    out: list[Chunk] = []
    for start in range(0, len(lines), step):
        window = lines[start:start + c["lines"]]
        body = "\n".join(window).strip()
        if len(body) >= c["min_chars"]:
            out.append(Chunk(
                id=f"{doc_key}#L{start + 1}",
                start=start + 1,
                end=min(start + c["lines"], len(lines)),
                text=body[:c["max_chars"]],
            ))
        if start + c["lines"] >= len(lines):
            break
    return out
