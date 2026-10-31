"""Index naming and model descriptors.

Retrieval behaviour is fixed in code. What varies per deployment is the
embedding model, and that only affects collection names and the threshold
table keyed by model tag.
"""

from __future__ import annotations

import os

from agent_gateway.services.retrieval.types import ModelSpec

_MODEL_TAGS = {
    "sentence-transformers/all-MiniLM-L6-v2": "minilm384",
    "jinaai/jina-embeddings-v2-base-zh": "jinazh768",
}


#: Tag of the model whose index names predate versioning; it keeps the bare names.
LEGACY_TAG = "minilm384"


def index_name(base: str, tag: str) -> str:
    """Collection / table name for a model: the legacy tag keeps the bare name."""
    return base if tag == LEGACY_TAG else f"{base}_{tag}"


def model_tag(model_id: str) -> str:
    """Short tag used for index names and threshold tables."""
    known = _MODEL_TAGS.get(model_id)
    if known:
        return known
    return "".join(ch for ch in model_id.split("/")[-1] if ch.isalnum()).lower()[:16]


def model_spec(model_id: str, dims: int) -> ModelSpec:
    return ModelSpec(
        id=model_id,
        tag=model_tag(model_id),
        dims=dims,
        query_prefix=os.environ.get("EMBEDDER_QUERY_PREFIX", ""),
        doc_prefix=os.environ.get("EMBEDDER_DOC_PREFIX", ""),
    )
