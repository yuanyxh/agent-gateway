"""Export cross-repository contracts from the implementation.

Consumers (the macOS host, the Node server) read these files instead of scanning
source code, which cannot work once the repositories are separate.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_ENV_CALLS = {"get", "setdefault", "_json_env"}

#: Modules the host launches by name. Keys are the host-facing stable names
#: (`GatewayEntry` in the macOS host); renaming one is a contract change.
ENTRIES: dict[str, str] = {
    "server": "agent_gateway.services.mcp.server",
    "tokenProxy": "agent_gateway.services.proxy.token_proxy",
    "summarize": "agent_gateway.services.summarize.summarize",
    "astExtractor": "agent_gateway.scanner.ast_extractor",
    "knowledgeRefill": "agent_gateway.services.knowledge.knowledge_refill",
}


def _env_names(source: str) -> set[str]:
    names: set[str] = set()

    def add(value) -> None:
        if (
            isinstance(value, str)
            and value.isupper()
            and value.replace("_", "").isalnum()
        ):
            names.add(value)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            attr = getattr(func, "attr", None) or getattr(func, "id", None)
            if attr in _ENV_CALLS and isinstance(node.args[0], ast.Constant):
                add(node.args[0].value)
        elif isinstance(node, ast.Assign):
            # `NAME_ENV_KEY = "X"` and `env["X"] = ...` are contracts too; the host injects them.
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_ENV_KEY"):
                    add(getattr(node.value, "value", None))
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "env"
                ):
                    add(getattr(target.slice, "value", None))
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


def _module_path(module: str) -> Path:
    relative = module.removeprefix("agent_gateway.").replace(".", "/")
    return PACKAGE_ROOT / (relative + ".py")


def check_entries() -> list[str]:
    """Every entry must exist and expose a **sync** top-level `main`.

    An `async def main` yields an un-awaited coroutine when the host calls `main()`,
    and the process exits immediately — the failure is invisible from this side.
    """
    problems: list[str] = []
    for name, module in ENTRIES.items():
        path = _module_path(module)
        if not path.is_file():
            problems.append(f"{name}: missing module {module} ({path})")
            continue
        functions = {
            node.name: isinstance(node, ast.AsyncFunctionDef)
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "main" not in functions:
            problems.append(f"{name}: {module} has no top-level main()")
        elif functions["main"]:
            problems.append(f"{name}: {module}.main must be sync (use asyncio.run)")
    return problems


def export(dest: Path | None = None) -> Path:
    """Write `env.json` and `entries.json` into `dest`; defaults to `<cwd>/contracts`."""
    problems = check_entries()
    if problems:
        raise SystemExit("entry contract broken:\n" + "\n".join(problems))
    target = dest or (Path.cwd() / "contracts")
    target.mkdir(parents=True, exist_ok=True)
    env_path = target / "env.json"
    env_path.write_text(
        json.dumps(
            {"package": "agent-gateway", "env": collect_env_names()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    entries_path = target / "entries.json"
    entries_path.write_text(
        json.dumps(
            {"package": "agent-gateway", "entries": ENTRIES},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target
