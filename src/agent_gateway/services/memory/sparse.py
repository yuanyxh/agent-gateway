"""Sparse engram codes: fixed random projection plus a context key."""

from __future__ import annotations

import hashlib

import numpy as np

DIMS = 2048
ACTIVE_BITS = 64
CONTEXT_BITS = 16
SEED = 20261111

_projection: dict[tuple[int, int], np.ndarray] = {}


def _matrix(input_dims: int, seed: int) -> np.ndarray:
    key = (input_dims, seed)
    cached = _projection.get(key)
    if cached is None:
        rng = np.random.default_rng(seed)
        cached = rng.standard_normal((DIMS, input_dims), dtype=np.float32)
        _projection[key] = cached
    return cached


def _context_seed(key: str, seed: int) -> int:
    digest = hashlib.sha1(f"{seed}|{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def context_key(episode_id: str = "", source: str = "", created_at: str = "") -> str:
    return f"{episode_id or ''}|{source or ''}|{str(created_at or '')[:7]}"


def encode(
    vector,
    context: str = "",
    *,
    k: int = ACTIVE_BITS,
    context_bits: int = CONTEXT_BITS,
    seed: int = SEED,
) -> bytes:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.size == 0:
        raise ValueError("empty vector")
    scores = _matrix(values.size, seed) @ values
    k = min(k, scores.size)
    index = np.argpartition(scores, -k)[-k:]
    bits = np.zeros(DIMS, dtype=np.uint8)
    bits[index] = 1
    if context:
        rng = np.random.default_rng(_context_seed(context, seed))
        bits[rng.choice(DIMS, size=min(context_bits, DIMS), replace=False)] = 1
    return np.packbits(bits).tobytes()


def decode(blob: bytes | None) -> np.ndarray:
    if not blob:
        return np.zeros(DIMS, dtype=np.uint8)
    packed = np.frombuffer(blob, dtype=np.uint8)
    return np.unpackbits(packed)[:DIMS]


def jaccard(left: np.ndarray, right: np.ndarray) -> float:
    union = int(np.bitwise_or(left, right).sum())
    if union == 0:
        return 0.0
    return int(np.bitwise_and(left, right).sum()) / union
