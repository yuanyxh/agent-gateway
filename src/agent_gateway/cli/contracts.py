"""Export cross-repository contracts from the implementation.

Consumers (the macOS host, the Node server) read these files instead of scanning
source code, which cannot work once the repositories are separate.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_ENV_CALLS = {"get", "setdefault"}


def _env_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        attr = getattr(func, "attr", None) or getattr(func, "id", None)
        if attr not in _ENV_CALLS:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            continue
        value = arg.value
        if value.isupper() and value.replace("_", "").isalnum():
            names.add(value)
    return names


def collect_env_names() -> list[str]:
    names: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            names |= _env_names(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
    return sorted(names)


def export(dest: Path | None = None) -> Path:
    """Write `env.json` into `dest`; defaults to `<cwd>/contracts`."""
    target = dest or (Path.cwd() / "contracts")
    target.mkdir(parents=True, exist_ok=True)
    path = target / "env.json"
    payload = {"package": "agent-gateway", "env": collect_env_names()}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
