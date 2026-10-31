"""Optional cross-encoder re-ranking.

Re-ranking is opt-in per profile: nothing here loads unless a caller asks for
it. A missing model or a failed load returns `None`, leaving the fused order
untouched, because ranking quality must not become a availability risk.
"""

from __future__ import annotations

import os
import threading
from dataclasses import replace

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_cache
from agent_gateway.services.retrieval.types import Query, RetrievedItem

logger = setup_logging("server")

MODEL_NAME = "bge-reranker-base-int8"

#: Position embeddings stop at 514; longer documents must be clipped.
MAX_TOKENS = 512

_lock = threading.Lock()
_instance: object | None = None
_failed = False


#: The cross-encoder emits a relevance logit: negative means "not a passage for
#: this query". Returning those buys tokens and nothing else, so they are cut.
MIN_SCORE = 0.0


def order_by_score(
    scores: list[float], items: list[RetrievedItem]
) -> list[RetrievedItem]:
    """Highest score first, dropping everything the model scored non-positive."""
    ranked = [(score, item) for score, item in zip(scores, items) if score > MIN_SCORE]
    ranked.sort(key=lambda pair: -pair[0])
    return [
        replace(item, score=float(score), score_kind="rerank") for score, item in ranked
    ]


def model_dir() -> str:
    explicit = os.environ.get("RERANK_MODEL_DIR", "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(str(gateway_cache()), "models", MODEL_NAME)


class CrossEncoderReranker:
    """Score every (query, document) pair and order by that score."""

    name = MODEL_NAME

    def __init__(self, directory: str | None = None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        root = directory or model_dir()
        self._np = np
        self._tok = Tokenizer.from_file(os.path.join(root, "tokenizer.json"))
        self._tok.enable_truncation(max_length=MAX_TOKENS)
        options = ort.SessionOptions()
        options.enable_cpu_mem_arena = False
        options.intra_op_num_threads = 4
        self._session = ort.InferenceSession(
            os.path.join(root, "onnx", "model_int8.onnx"),
            options,
            providers=["CPUExecutionProvider"],
        )

    def __call__(self, query: Query, items: list[RetrievedItem]) -> list[RetrievedItem]:
        if not items:
            return items
        encodings = [self._tok.encode(query.text, row.content) for row in items]
        width = max(len(enc.ids) for enc in encodings)
        input_ids = self._np.zeros((len(encodings), width), dtype=self._np.int64)
        attention = self._np.zeros((len(encodings), width), dtype=self._np.int64)
        for index, enc in enumerate(encodings):
            input_ids[index, : len(enc.ids)] = enc.ids
            attention[index, : len(enc.ids)] = 1
        logits = self._session.run(
            ["logits"], {"input_ids": input_ids, "attention_mask": attention}
        )[0]
        scores = logits.reshape(len(items), -1)[:, 0]
        return order_by_score([float(s) for s in scores], items)


def get_reranker() -> CrossEncoderReranker | None:
    """The shared reranker, or None when the model is unavailable."""
    global _instance, _failed
    if _instance is not None or _failed:
        return _instance  # type: ignore[return-value]
    with _lock:
        if _instance is not None or _failed:
            return _instance  # type: ignore[return-value]
        try:
            _instance = CrossEncoderReranker()
        except Exception as e:  # noqa: BLE001 - degrade to no re-ranking
            _failed = True
            logger.warning(
                f"[rerank] unavailable at {model_dir()}, keeping fused order: {e!r}"
            )
    return _instance  # type: ignore[return-value]


__all__ = [
    "MAX_TOKENS",
    "MIN_SCORE",
    "MODEL_NAME",
    "CrossEncoderReranker",
    "get_reranker",
    "model_dir",
    "order_by_score",
]
