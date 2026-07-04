"""Deterministic, dependency-free embedder for the plugin eval harness.

bean's `embedding.plugin` hook loads a module exposing `embed(texts) -> list[list[float]]`
(and optionally `embed_query`). Pointing the fixture *and* the live CLI at this file means both
sides embed with the exact same function — so vector search is reproducible and needs no model
download. It's a hashed bag-of-tokens projection: shared tokens → overlapping dimensions →
cosine similarity, which is all the harness needs to check that retrieval surfaces the right doc.
"""

from __future__ import annotations

import hashlib
import math
import re

DIM = 128
_TOKEN = re.compile(r"[a-z0-9]+")


def _vec(text: str) -> list[float]:
    v = [0.0] * DIM
    for tok in _TOKEN.findall((text or "").lower()):
        h = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:8], "big")
        idx = h % DIM
        sign = 1.0 if (h >> 7) & 1 else -1.0
        v[idx] += sign
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return [1.0 / math.sqrt(DIM)] * DIM  # never hand back a zero vector
    return [x / norm for x in v]


def embed(texts) -> list[list[float]]:
    return [_vec(t) for t in texts]


def embed_query(text) -> list[float]:
    return _vec(text)
