"""Keep evaluation scripts from writing into the live gateway cache."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "tests" / "eval" / ".cache"


def isolate_cache(script: str) -> str:
    """Point GATEWAY_CACHE at a per-script directory under tests/eval/.cache."""
    override = os.environ.get("EVAL_CACHE_DIR", "").strip()
    root = Path(override).expanduser() if override else DEFAULT_ROOT
    target = root / Path(script).stem
    target.mkdir(parents=True, exist_ok=True)
    os.environ["GATEWAY_CACHE"] = str(target)
    return str(target)
