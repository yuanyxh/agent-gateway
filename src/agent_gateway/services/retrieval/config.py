"""Profile switches.

New stages default to off so the migrated pipeline is behaviour-identical until
a profile is enabled explicitly.
"""

from __future__ import annotations

import os

from agent_gateway.core import config_loader
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


def _setting(env_key: str, yaml_key: str, default: str) -> str:
    """Env wins, then config.yaml, then default."""
    raw = os.environ.get(env_key)
    if raw is not None:
        return raw.strip()
    node = config_loader.value(yaml_key, default)
    if isinstance(node, list):
        return ",".join(str(item) for item in node)
    return str(node).strip() if node is not None else default


def gate_mode(profile: str = "") -> str:
    return _setting("RETRIEVAL_GATE", "retrieval.gate", "layer") or "layer"


def stage_number(
    profile: str,
    stage: str,
    name: str,
    default: float,
) -> float:
    """Numeric knob for one stage, overridable without a code change.

    Lookup order: `RETRIEVAL_<PROFILE>_<STAGE>_<NAME>` env, then
    `retrieval.plans.<profile>.<stage>.<name>` in config.yaml, then default.
    Values live outside the code because the right number depends on the data
    a deployment holds, not on the algorithm.
    """
    key = f"RETRIEVAL_{profile}_{stage}_{name}".upper().replace("-", "_")
    yaml_key = f"retrieval.plans.{profile}.{stage}.{name}"
    raw = _setting(key, yaml_key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def stage_flag(profile: str, name: str, default: bool) -> bool:
    """Boolean knob for one profile, same lookup order as `stage_number`.

    `run_all` turns the tiered walk into a single tier: every arm runs and the
    fused list is ranked once, instead of stopping at the first acceptable tier.
    """
    key = f"RETRIEVAL_{profile}_{name}".upper().replace("-", "_")
    raw = _setting(key, f"retrieval.plans.{profile}.{name}", "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def keyword_mode(profile: str, default: str = "off") -> str:
    """How caller keywords take part in retrieval.

    `off` keeps the legacy re-rank boost only. `first` gives keywords their own
    tier above the raw query. `merge` runs both inside one tier and adds up the
    per-keyword scores. `scope` lets keywords narrow the query's hits, which is
    what a range limit means: all keywords intersect, then any, then none.

    YAML 1.1 reads the bare word `off` as boolean false, so the value is
    normalised before the string match or the setting silently falls back.
    """
    env_key = f"RETRIEVAL_{profile}_KEYWORD_MODE"
    raw = os.environ.get(env_key)
    if raw is None:
        # Root key first: the Swift config model can hold a string there but
        # not inside `plans`, whose leaves are numbers.
        raw = config_loader.value(f"retrieval.{profile}KeywordMode", None)
    if raw is None:
        raw = config_loader.value(f"retrieval.plans.{profile}.keyword_mode", default)
    if isinstance(raw, bool):
        return "first" if raw else "off"
    value = str(raw).strip().lower()
    return value if value in ("off", "first", "merge", "scope") else default


def notes_semantic_floor(default: float) -> float:
    """Semantic re-check floor for lexical note hits; 0 disables the check."""
    raw = _setting("RETRIEVAL_NOTES_SEMANTIC", "retrieval.notesSemantic", "")
    if raw in ("", str(default)):
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)
