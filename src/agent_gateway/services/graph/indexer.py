#!/usr/bin/env python3
"""Resident graph indexer: JSON Lines over stdin/stdout.

Requests: {"op": "ping"}, {"op": "index_file", ...}, {"op": "index_batch", ...}
Every response is one JSON object; failures are ok:false with a stable reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
import uuid

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.graph import resolver
from agent_gateway.services.graph.model import FileAnalysis, ParseError
from agent_gateway.services.graph.parsers import EXTENSIONS, PARSERS

ENGINE_VERSION = "2"
SCHEMA_VERSION = "1"

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".build",
        ".gradle",
        ".next",
        ".nuxt",
        ".utility",
        ".venv",
        "DerivedData",
        "Pods",
        "__pycache__",
        "coverage",
        "dist",
        "node_modules",
    }
)

logger = setup_logging("graph", json_format=True)
_INDEXES: dict[str, "ProjectIndex"] = {}


class ProtocolError(Exception):
    """A request-level failure that maps to a stable reason string."""

    def __init__(self, reason: str, detail: str = "", path: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.path = path


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _walk_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in EXTENSIONS:
                yield os.path.join(dirpath, name)


class ProjectIndex:
    """In-memory symbol table for one project root."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.analyses: dict[str, FileAnalysis] = {}
        self.by_name: dict[str, list] = {}
        self.file_paths: set[str] = set()
        self.exports: dict[str, list[str]] = {}
        self.failed_files: list[str] = []
        self._hashes: dict[str, str] = {}
        self._langs: dict[str, str] = {}
        self._loaded = False

    def prepare(self, paths: set[str]) -> int:
        """Load exactly the host's file set; drop files the host no longer has."""
        for path in list(self.analyses):
            if path not in paths:
                self.remove(path)
        self._loaded = True
        for rel in sorted(paths):
            if rel in self.analyses:
                continue
            lang = EXTENSIONS.get(os.path.splitext(rel)[1].lower())
            if lang is None or lang not in PARSERS:
                continue
            try:
                with open(os.path.join(self.root, rel), "rb") as handle:
                    data = handle.read()
                analysis = PARSERS[lang](data, rel)
            except Exception as exc:
                self.failed_files.append(rel)
                logger.warning(
                    json.dumps(
                        {"event": "index_failed", "file": rel, "error": str(exc)}
                    )
                )
                continue
            self.analyses[rel] = analysis
            self._hashes[rel] = _sha1(data)
            self._langs[rel] = lang
            self._add_symbols(analysis)
        return len(self.analyses)

    def remove(self, path: str) -> None:
        """Removes a file, or every file under a directory path."""
        prefix = path.rstrip("/") + "/"
        for rel in [
            item for item in self.analyses if item == path or item.startswith(prefix)
        ]:
            analysis = self.analyses.pop(rel)
            self._drop_symbols(analysis)
            self._hashes.pop(rel, None)
            self._langs.pop(rel, None)
            self.file_paths.discard(rel)
        self._hashes.pop(path, None)
        self._langs.pop(path, None)
        self.file_paths.discard(path)

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        for full in _walk_files(self.root):
            rel = os.path.relpath(full, self.root).replace(os.sep, "/")
            lang = EXTENSIONS.get(os.path.splitext(rel)[1].lower())
            if lang is None or lang not in PARSERS:
                continue
            try:
                with open(full, "rb") as handle:
                    data = handle.read()
                analysis = PARSERS[lang](data, rel)
            except Exception as exc:
                self.failed_files.append(rel)
                logger.warning(
                    json.dumps(
                        {"event": "index_failed", "file": rel, "error": str(exc)}
                    )
                )
                continue
            self.analyses[rel] = analysis
            self._hashes[rel] = _sha1(data)
            self._add_symbols(analysis)
        self._loaded = True

    def _add_symbols(self, analysis: FileAnalysis) -> None:
        self.file_paths.add(analysis.path)
        for symbol in analysis.symbols:
            self.by_name.setdefault(symbol.name, []).append(symbol)
        exported = [
            item.raw_name for item in analysis.references if item.kind == "export"
        ]
        if exported:
            self.exports[analysis.path] = exported

    def _drop_symbols(self, analysis: FileAnalysis) -> None:
        self.exports.pop(analysis.path, None)
        for name in {symbol.name for symbol in analysis.symbols}:
            remaining = [
                item
                for item in self.by_name.get(name, ())
                if item.path != analysis.path
            ]
            if remaining:
                self.by_name[name] = remaining
            else:
                self.by_name.pop(name, None)

    def apply(self, analysis: FileAnalysis, digest: str) -> None:
        previous = self.analyses.get(analysis.path)
        if previous is not None:
            self._drop_symbols(previous)
        self.analyses[analysis.path] = analysis
        self._hashes[analysis.path] = digest
        self._add_symbols(analysis)

    def analyze_bytes(
        self, rel_path: str, lang: str, data: bytes
    ) -> tuple[FileAnalysis, str]:
        digest = _sha1(data)
        cached = self.analyses.get(rel_path)
        if (
            cached is not None
            and self._hashes.get(rel_path) == digest
            and self._langs.get(rel_path) == lang
        ):
            return cached, digest
        analysis = PARSERS[lang](data, rel_path)
        self._langs[rel_path] = lang
        return analysis, digest


def _index_for(req: dict) -> ProjectIndex:
    root = req.get("root")
    if not isinstance(root, str) or not root.strip():
        raise ProtocolError("bad_request", "root is required")
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise ProtocolError("root_not_found", f"not a directory: {root}")
    index = _INDEXES.get(root)
    if index is None:
        index = ProjectIndex(root)
        _INDEXES[root] = index
    return index


def _normalize_path(index: ProjectIndex, raw: object) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("bad_request", "path is required")
    rel = raw.replace("\\", "/")
    if os.path.isabs(rel):
        rel = os.path.relpath(rel, index.root)
    rel = os.path.normpath(rel).replace(os.sep, "/")
    if rel == ".." or rel.startswith("../") or os.path.isabs(rel):
        raise ProtocolError("path_outside_root", f"path escapes root: {raw}")
    full = os.path.realpath(os.path.join(index.root, rel))
    if full != index.root and not full.startswith(index.root + os.sep):
        raise ProtocolError("path_outside_root", f"path escapes root: {raw}")
    return rel, full


def _language(req: dict, rel_path: str) -> str:
    explicit = req.get("lang")
    if explicit:
        if explicit not in PARSERS:
            raise ProtocolError(
                "unsupported_language", f"unsupported language: {explicit}"
            )
        return str(explicit)
    ext = os.path.splitext(rel_path)[1].lower()
    lang = EXTENSIONS.get(ext)
    if lang is None:
        raise ProtocolError(
            "unsupported_language", f"no parser for extension: {ext or '(none)'}"
        )
    return lang


def _read_source(index: ProjectIndex, rel_path: str, full: str, req: dict) -> bytes:
    if not os.path.isfile(full):
        raise ProtocolError("file_not_found", f"not a file: {rel_path}", path=rel_path)
    with open(full, "rb") as handle:
        data = handle.read()
    expected = req.get("content_hash")
    if isinstance(expected, str) and expected:
        if _sha1(data) != expected:
            raise ProtocolError(
                "content_hash_mismatch",
                f"content hash differs for {rel_path}",
                path=rel_path,
            )
    return data


def _analyze(
    index: ProjectIndex, rel_path: str, lang: str, data: bytes
) -> tuple[FileAnalysis, str]:
    try:
        return index.analyze_bytes(rel_path, lang, data)
    except ParseError as exc:
        raise ProtocolError(exc.reason, exc.detail, path=rel_path) from exc
    except Exception as exc:  # noqa: BLE001 - normalize parser crashes per file
        raise ProtocolError("parse_failed", str(exc), path=rel_path) from exc


def _symbol_dict(symbol) -> dict:
    return {
        "name": symbol.name,
        "kind": symbol.kind,
        "signature": symbol.signature,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
    }


def _result(index: ProjectIndex, analysis: FileAnalysis) -> dict:
    edges, unresolved = resolver.resolve_file(analysis, index)
    return {
        "ok": True,
        "path": analysis.path,
        "lang": analysis.lang,
        "symbols": [_symbol_dict(symbol) for symbol in analysis.symbols],
        "edges": edges,
        "unresolved": unresolved,
    }


def _index_file(req: dict) -> dict:
    index = _index_for(req)
    rel_path, full = _normalize_path(index, req.get("path"))
    lang = _language(req, rel_path)
    data = _read_source(index, rel_path, full, req)
    index.ensure_loaded()
    analysis, digest = _analyze(index, rel_path, lang, data)
    index.apply(analysis, digest)
    return _result(index, analysis)


def _index_batch(req: dict) -> dict:
    files = req.get("files")
    if not isinstance(files, list) or not files:
        raise ProtocolError("bad_request", "files must be a non-empty list")
    index = _index_for(req)
    index.ensure_loaded()
    results: list[dict | None] = [None] * len(files)
    parsed: list[tuple[int, FileAnalysis, str]] = []
    for position, item in enumerate(files):
        path = item.get("path") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise ProtocolError("bad_request", "each file entry must be an object")
            rel_path, full = _normalize_path(index, item.get("path"))
            lang = _language(item, rel_path)
            data = _read_source(index, rel_path, full, item)
            analysis, digest = _analyze(index, rel_path, lang, data)
            parsed.append((position, analysis, digest))
        except ProtocolError as exc:
            results[position] = {
                "ok": False,
                "path": path,
                "error": exc.detail,
                "reason": exc.reason,
            }
        except Exception as exc:
            results[position] = {
                "ok": False,
                "path": path,
                "error": str(exc),
                "reason": "parse_failed",
            }
    # Apply the whole batch before resolving: bindings must not depend on input order.
    for _, analysis, digest in parsed:
        index.apply(analysis, digest)
    for position, analysis, _ in parsed:
        results[position] = _result(index, analysis)
    return {"ok": True, "op": "index_batch", "results": results}


def _prepare(req: dict) -> dict:
    index = _index_for(req)
    raw = req.get("paths")
    if not isinstance(raw, list):
        raise ProtocolError("bad_request", "paths must be a list")
    paths: set[str] = set()
    for item in raw:
        rel_path, _ = _normalize_path(index, item)
        paths.add(rel_path)
    return {"ok": True, "op": "prepare", "count": index.prepare(paths)}


def _remove_file(req: dict) -> dict:
    index = _index_for(req)
    rel_path, _ = _normalize_path(index, req.get("path"))
    index.remove(rel_path)
    return {"ok": True, "op": "remove_file", "path": rel_path}


def _dispatch(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {
            "ok": True,
            "op": "ping",
            "engine_version": ENGINE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "languages": sorted(PARSERS),
            "pid": os.getpid(),
        }
    if op == "index_file":
        return _index_file(req)
    if op == "index_batch":
        return _index_batch(req)
    if op == "prepare":
        return _prepare(req)
    if op == "remove_file":
        return _remove_file(req)
    raise ProtocolError("unknown_op", f"unknown op: {op!r}")


def _log(trace_id: str, req: dict, response: dict, elapsed_ms: float) -> None:
    edge_count = len(response.get("edges", ()))
    unresolved_count = len(response.get("unresolved", ()))
    for item in response.get("results", ()):
        edge_count += len(item.get("edges", ()))
        unresolved_count += len(item.get("unresolved", ()))
    logger.info(
        json.dumps(
            {
                "trace_id": trace_id,
                "op": req.get("op"),
                "project": req.get("root"),
                "file": req.get("path"),
                "elapsed_ms": round(elapsed_ms, 2),
                "edge_count": edge_count,
                "unresolved_count": unresolved_count,
            }
        )
    )


def handle_line(raw: str) -> dict:
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    req: dict = {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ProtocolError("bad_request", "request must be a JSON object")
        req = parsed
        response = _dispatch(req)
    except ProtocolError as exc:
        response = {
            "ok": False,
            "op": req.get("op"),
            "error": exc.detail,
            "reason": exc.reason,
        }
        path = exc.path or req.get("path")
        if path:
            response["path"] = path
    except json.JSONDecodeError as exc:
        response = {
            "ok": False,
            "error": f"invalid json: {exc}",
            "reason": "bad_request",
        }
    except Exception as exc:
        logger.error(traceback.format_exc())
        response = {
            "ok": False,
            "op": req.get("op"),
            "error": str(exc),
            "reason": "internal_error",
        }
    _log(trace_id, req, response, (time.perf_counter() - started) * 1000)
    return response


def main() -> int:
    """Read requests until stdin closes; stdout carries protocol lines only."""
    for raw in sys.stdin.buffer:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        response = handle_line(text)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
