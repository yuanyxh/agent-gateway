#!/usr/bin/env python3
"""
AST-based symbol extractor — dispatch layer.

Usage (single file):
  echo "content" | python3 ast_extractor.py --ext swift --file-path a.swift

Usage (batch):
  echo '[{"content": "...", "file_path": "a.vue", "ext": "vue"}, ...]' \\
    | python3 ast_extractor.py --batch

Output (single):  JSON {"symbols": [...], "imports": [...]} or {"error": "..."}
Output (batch):   JSON {"file_path": {"symbols": [...], "imports": [...]}, "file_path": {"error": "..."}}
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

# Resource lookup works both from the source tree and an installed wheel.
AST_JS_DIR = Path(str(files("agent_gateway.scanner") / "ast-js"))


# ── Python AST extractor ──

PY_KINDS = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "function",
    ast.ClassDef: "class",
}


def _py_name(node) -> str:
    return getattr(node, "name", "(anonymous)")


def _py_params(node) -> list[str]:
    args = getattr(node, "args", None)
    if args is None:
        return []
    params = []
    for a in args.args if hasattr(args, "args") else (args or []):
        params.append(a.arg if hasattr(a, "arg") else str(a))
    # Handle *args
    if hasattr(args, "vararg") and args.vararg:
        params.append("*" + args.vararg.arg)
    # Handle **kwargs
    if hasattr(args, "kwarg") and args.kwarg:
        params.append("**" + args.kwarg.arg)
    return params


def _py_return_annotation(node) -> str | None:
    """Extract return type annotation as a string."""
    ret = getattr(node, "returns", None)
    if ret is None:
        return None
    return _py_type_annotation(ret)


def _py_type_annotation(anno) -> str:
    """Convert an ast type annotation node to a readable string."""
    if anno is None:
        return ""
    if isinstance(anno, ast.Name):
        return anno.id
    if isinstance(anno, ast.Subscript):
        value = _py_type_annotation(anno.value)
        if hasattr(anno, "slice") and getattr(anno.slice, "id", None):
            return f"{value}[{anno.slice.id}]"
        # Python 3.9+ uses ast.Slice with lower/upper
        return f"{value}[...]"
    if isinstance(anno, ast.Attribute):
        return f"{_py_type_annotation(anno.value)}.{anno.attr}"
    if isinstance(anno, ast.Constant):
        return str(anno.value)
    if isinstance(anno, ast.Tuple):
        elts = ", ".join(_py_type_annotation(e) for e in anno.elts)
        return f"({elts})"
    if isinstance(anno, ast.BinOp):
        left = _py_type_annotation(anno.left)
        op = " | " if isinstance(anno.op, ast.BitOr) else " "
        right = _py_type_annotation(anno.right)
        return f"{left}{op}{right}"
    if isinstance(anno, ast.List):
        elts = ", ".join(_py_type_annotation(e) for e in anno.elts)
        return f"[{elts}]"
    return str(anno)


def _py_signature(node) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        params = _py_params(node)
        sig = _py_name(node) + "(" + ", ".join(params) + ")"
        ret = _py_return_annotation(node)
        if ret:
            sig += " -> " + ret
        return sig
    if isinstance(node, ast.ClassDef):
        bases = [b for b in node.bases if isinstance(b, ast.Name)]
        if bases:
            return _py_name(node) + "(" + ", ".join(b.id for b in bases) + ")"
        return _py_name(node)
    return None


def _py_docstring(node) -> str | None:
    """Extract the docstring from a function/class/module node."""
    body = getattr(node, "body", [])
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0].value.value.strip()
    return None


def _py_decorator_name(decorator) -> str | None:
    """Extract decorator name as string from an ast decorator node."""
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return (
            _py_decorator_name(decorator.value) + "." + decorator.attr
            if decorator.value
            else decorator.attr
        )
    if isinstance(decorator, ast.Call):
        return _py_decorator_name(decorator.func)
    return None


def _extract_python(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """Extract symbols and file imports from Python code using ast module."""
    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError as e:
        raise _ASTError(f"Python syntax error: {e}")

    symbols: list[dict] = []

    def _walk(node, depth=0):
        """Walk the AST and collect top-level declarations and class members."""
        if isinstance(node, ast.Module):
            for child in node.body:
                _walk(child, depth)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only collect at module level or class level
            name = _py_name(node)
            kind = PY_KINDS.get(type(node), "function")
            doc = _py_docstring(node)
            sig = _py_signature(node)

            entry = {
                "name": name,
                "kind": kind,
                "start_line": node.lineno,
                "end_line": node.end_lineno,
            }
            if sig:
                entry["signature"] = sig
            if doc:
                entry["doc_comment"] = doc

            # Collect decorators
            decorators = []
            for d in getattr(node, "decorator_list", []):
                dn = _py_decorator_name(d)
                if dn:
                    decorators.append(dn)
            if decorators:
                entry["decorators"] = decorators

            symbols.append(entry)

            # Walk body for nested (not normally needed, but complete)
            for child in getattr(node, "body", []):
                _walk(child, depth + 1)

        elif isinstance(node, ast.ClassDef):
            sig = _py_signature(node)
            doc = _py_docstring(node)
            entry = {
                "name": _py_name(node),
                "kind": "class",
                "start_line": node.lineno,
                "end_line": node.end_lineno,
            }
            if sig:
                entry["signature"] = sig
            if doc:
                entry["doc_comment"] = doc

            # Decorators
            decorators = []
            for d in getattr(node, "decorator_list", []):
                dn = _py_decorator_name(d)
                if dn:
                    decorators.append(dn)
            if decorators:
                entry["decorators"] = decorators

            symbols.append(entry)

            # Walk class body for methods
            for child in getattr(node, "body", []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_name = _py_name(child)
                    method_kind = "method"
                    # Detect @property, @staticmethod, @classmethod
                    for d in getattr(child, "decorator_list", []):
                        dn = _py_decorator_name(d)
                        if dn in ("property",):
                            method_kind = "property"
                        elif dn in ("staticmethod", "classmethod"):
                            method_kind = dn
                    sig = _py_signature(child)
                    method_entry = {
                        "name": method_name,
                        "kind": method_kind,
                        "start_line": child.lineno,
                        "end_line": child.end_lineno,
                    }
                    if sig:
                        method_entry["signature"] = sig
                    doc = _py_docstring(child)
                    if doc:
                        method_entry["doc_comment"] = doc
                    symbols.append(method_entry)

        elif isinstance(node, ast.Assign):
            # Module-level constant (UPPER_CASE) or simple name
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name.isupper() or depth == 0:
                        kind = "constant" if name.isupper() else "variable"
                        entry = {
                            "name": name,
                            "kind": kind,
                            "start_line": node.lineno,
                            "end_line": node.end_lineno,
                        }
                        # Try to get type annotation via value type
                        symbols.append(entry)

        elif isinstance(node, ast.AnnAssign):
            # Annotated assignment: x: int = 5
            if isinstance(node.target, ast.Name) and depth == 0:
                name = node.target.id
                kind = "constant" if name.isupper() else "variable"
                entry = {
                    "name": name,
                    "kind": kind,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }
                # Get type annotation
                anno_str = (
                    _py_type_annotation(node.annotation) if node.annotation else None
                )
                if anno_str:
                    entry["signature"] = f"{name}: {anno_str}"
                symbols.append(entry)

        elif isinstance(node, ast.AsyncFor):
            # Skip body of async for (depth control)
            pass

    _walk(tree)
    try:
        imports = _py_imports(tree)
    except Exception:
        imports = []
    return symbols, imports


def _py_imports(tree) -> list[dict]:
    """Collect `import` / `from ... import` statements; relative levels keep their dots."""
    imports: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    {"raw_target": alias.name, "kind": "static", "line": node.lineno}
                )
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            if node.module:
                imports.append(
                    {
                        "raw_target": prefix + node.module,
                        "kind": "static",
                        "line": node.lineno,
                    }
                )
            elif node.level:
                # `from . import util` imports the submodule, not the package.
                for alias in node.names:
                    if alias.name != "*":
                        imports.append(
                            {
                                "raw_target": prefix + alias.name,
                                "kind": "static",
                                "line": node.lineno,
                            }
                        )
    return imports


# ── Error propagation ──


class _ASTError(Exception):
    """Raised when AST parsing fails (syntax error, parse timeout, etc.).
    Caught by main() → outputs {"error": "..."} with exit code 1,
    signalling the Swift side to fall back to regex."""

    pass


def _raise_if_error(result):
    """If result dict contains 'error', raise _ASTError."""
    if isinstance(result, dict) and "error" in result:
        raise _ASTError(result["error"])


# ── JS/TS/Vue extractor via Node.js ──


def _find_node() -> str:
    """Find node binary from ast-js/node_modules/.bin or system PATH."""
    local_node = AST_JS_DIR / "node_modules" / ".bin" / "node"
    if local_node.exists():
        return str(local_node)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "node"
        if candidate.exists():
            return str(candidate)
    return "node"


def _ensure_js_deps() -> bool:
    """Check if @babel/parser is installed, if not try to install."""
    node_modules = AST_JS_DIR / "node_modules"
    parser_dir = node_modules / "@babel" / "parser"
    if parser_dir.exists():
        return True
    # Try to auto-install
    try:
        subprocess.run(
            [
                "npm",
                "install",
                "--prefix",
                str(AST_JS_DIR),
                "--production",
                "--no-audit",
            ],
            capture_output=True,
            timeout=60,
        )
        return parser_dir.exists()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _call_node_parser(content: str, ext: str) -> list[dict] | dict:
    """Call Node.js parse.mjs as subprocess and return parsed result."""
    parse_script = AST_JS_DIR / "parse.mjs"
    if not parse_script.exists():
        return {"error": "parse.mjs not found"}

    if not _ensure_js_deps():
        return {"error": "@babel/parser not installed"}

    try:
        proc = subprocess.run(
            [_find_node(), str(parse_script), ext],
            input=content,
            capture_output=True,
            timeout=30,
            text=True,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"error": "Node.js parser timed out"}
    except FileNotFoundError:
        return {"error": "node binary not found"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200]
        return {"error": f"node exited {proc.returncode}: {err}"}

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "invalid JSON from node parser"}

    return result


def _extract_vue(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """Extract the first script block as symbols, and imports from every script block."""
    matches = list(re.finditer(r"<script\b([^>]*)>(.*?)</script>", content, re.DOTALL))
    if not matches:
        return [], []

    symbols: list[dict] = []
    imports: list[dict] = []
    first = matches[0]
    for idx, m in enumerate(matches):
        attrs = m.group(1)
        raw = m.group(2)
        script_content = raw.strip()
        if not script_content:
            continue

        # Shift parser-relative lines back to the original file: newlines before
        # the block content plus the block's stripped leading whitespace.
        lead_newlines = raw[: len(raw) - len(raw.lstrip())].count("\n")
        offset = content[: m.start(2)].count("\n") + lead_newlines

        is_ts = bool(re.search(r'lang\s*=\s*["\']ts["\']', attrs))
        ext = "tsx" if is_ts else "jsx"
        result = _call_node_parser(script_content, ext)
        _raise_if_error(result)
        if not isinstance(result, dict):
            continue

        if idx == first and not symbols:
            symbols = result.get("symbols", [])
            for s in symbols:
                if "start_line" in s:
                    s["start_line"] += offset
                if "end_line" in s:
                    s["end_line"] += offset

        for imp in result.get("imports", []):
            if "line" in imp and imp["line"] is not None:
                imp["line"] += offset
            imports.append(imp)

    # Add component name from filename if not already extracted
    base = Path(file_path).stem
    already = {s.get("name") for s in symbols}
    if base not in already and not base.startswith("index"):
        script_tag_line = content[: first.start()].count("\n") + 1
        symbols.append(
            {
                "name": base,
                "kind": "component",
                "start_line": script_tag_line,
                "end_line": script_tag_line,
            }
        )

    return symbols, imports


def _extract_js_ts(content: str, ext: str) -> tuple[list[dict], list[dict]]:
    """Extract symbols and imports from JS/TS/JSX/TSX via the Node parser."""
    result = _call_node_parser(content, ext)
    _raise_if_error(result)
    if not isinstance(result, dict):
        return [], []
    return result.get("symbols", []), result.get("imports", [])


# ── Batch mode ──


def _extract_one(content: str, file_path: str, ext: str) -> dict:
    """Dispatch one file; returns {'symbols': [...], 'imports': [...]} or {'error': '...'}."""
    try:
        if ext == "py":
            symbols, imports = _extract_python(content, file_path)
        elif ext == "vue":
            symbols, imports = _extract_vue(content, file_path)
        elif ext in ("js", "ts", "jsx", "tsx", "mjs", "cjs"):
            effective_ext = "js" if ext in ("mjs", "cjs") else ext
            symbols, imports = _extract_js_ts(content, effective_ext)
        else:
            return {"error": f"unsupported extension: {ext}"}
        return {"symbols": symbols, "imports": imports[:2000]}
    except _ASTError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def _run_batch():
    """Read JSON array of file descriptors from stdin, extract all, write JSON result dict."""
    try:
        files = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid batch JSON: {e}"}))
        sys.exit(1)

    if not isinstance(files, list):
        print(json.dumps({"error": "batch input must be a JSON array"}))
        sys.exit(1)

    results = {}
    for f in files:
        fp = f.get("file_path", "")
        results[fp] = _extract_one(
            content=f.get("content", ""),
            file_path=fp,
            ext=f.get("ext", "").lower(),
        )

    print(json.dumps(results))


# ── Main ──


def main():
    parser = argparse.ArgumentParser(description="AST-based symbol extractor")
    parser.add_argument("--ext", help="File extension (required for single mode)")
    parser.add_argument(
        "--file-path", default="", help="Original file path (for context)"
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: stdin is a JSON array of {content, file_path, ext}",
    )
    args = parser.parse_args()

    if args.batch:
        _run_batch()
        return

    content = sys.stdin.read()

    if not args.ext:
        print(json.dumps({"error": "--ext is required in single mode"}))
        sys.exit(1)

    result = _extract_one(content, args.file_path, args.ext.lower())
    if "error" in result:
        print(json.dumps(result))
        sys.exit(1)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
