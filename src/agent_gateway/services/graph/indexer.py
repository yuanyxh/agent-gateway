#!/usr/bin/env python3
"""Resident graph indexer: JSON Lines over stdin/stdout.

Requests: {"op": "ping"}, {"op": "index_file", ...}, {"op": "index_batch", ...}
Every response is one JSON object; failures are ok:false with a stable reason.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import uuid

from agent_gateway.core import project_paths
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.graph import project_scan, resolver, store
from agent_gateway.services.graph.analysis import ProjectIndex, sha1
from agent_gateway.services.graph.constants import (
    ENGINE_VERSION as ENGINE_VERSION,
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from agent_gateway.services.graph.model import FileAnalysis, ParseError
from agent_gateway.services.graph.parsers import EXTENSIONS, PARSERS

logger = setup_logging("graph", json_format=True)
_INDEXES: dict[str, "ProjectIndex"] = {}


class ProtocolError(Exception):
    """A request-level failure that maps to a stable reason string."""

    def __init__(self, reason: str, detail: str = "", path: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.path = path


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
        if sha1(data) != expected:
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


def _store_path(index: ProjectIndex) -> str:
    path = str(project_paths.graph_db_path(index.root))
    if os.path.isfile(path):
        with store.GraphStore(path, readonly=True) as db:
            if db.is_up_to_date(index.root, ENGINE_VERSION, SCHEMA_VERSION):
                return path
    project_scan.sync(index.root, ENGINE_VERSION, SCHEMA_VERSION)
    return path


def _write_file(
    index: ProjectIndex,
    rel_path: str,
    lang: str,
    data: bytes,
    analysis: FileAnalysis,
    result: dict,
    db: store.GraphStore | None = None,
) -> None:
    full = os.path.join(index.root, rel_path)
    try:
        mtime = os.stat(full).st_mtime
    except OSError:
        mtime = 0.0
    symbols = [
        store.SymbolRow(
            sym_key=store.sym_key(
                rel_path, item.name, item.kind, item.signature, item.start_line
            ),
            path=rel_path,
            name=item.name,
            kind=item.kind,
            signature=item.signature,
            start_line=item.start_line,
            end_line=item.end_line,
        )
        for item in analysis.symbols
    ]
    file_row = store.FileRow(
        path=rel_path,
        lang=lang,
        size=len(data),
        mtime=mtime,
        content_hash=store.sha1(data),
    )
    edges = project_scan._edge_rows(analysis, result.get("edges") or [])
    unresolved = project_scan._unresolved_rows(analysis, result.get("unresolved") or [])
    if db is not None:
        db.replace_file(file_row, symbols, edges, unresolved)
        return
    with store.GraphStore(_store_path(index)) as store_db:
        store_db.replace_file(file_row, symbols, edges, unresolved)


def _index_file(req: dict) -> dict:
    index = _index_for(req)
    rel_path, full = _normalize_path(index, req.get("path"))
    lang = _language(req, rel_path)
    data = _read_source(index, rel_path, full, req)
    digest = store.sha1(data)
    db_path = str(project_paths.graph_db_path(index.root))
    if req.get("force") is False and os.path.isfile(db_path):
        with store.GraphStore(db_path, readonly=True) as db:
            if db.file_hash(rel_path) == digest:
                return {"ok": True, "path": rel_path, "unchanged": True}
    index.ensure_loaded()
    analysis, digest = _analyze(index, rel_path, lang, data)
    index.apply(analysis, digest)
    result = _result(index, analysis)
    _write_file(index, rel_path, lang, data, analysis, result)
    return result


def _index_batch(req: dict) -> dict:
    files = req.get("files")
    if not isinstance(files, list) or not files:
        raise ProtocolError("bad_request", "files must be a non-empty list")
    index = _index_for(req)
    index.ensure_loaded()
    results: list[dict | None] = [None] * len(files)
    parsed: list[tuple[int, FileAnalysis, str, str, bytes]] = []
    for position, item in enumerate(files):
        path = item.get("path") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise ProtocolError("bad_request", "each file entry must be an object")
            rel_path, full = _normalize_path(index, item.get("path"))
            lang = _language(item, rel_path)
            data = _read_source(index, rel_path, full, item)
            analysis, digest = _analyze(index, rel_path, lang, data)
            parsed.append((position, analysis, digest, lang, data))
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
    for _, analysis, digest, _, _ in parsed:
        index.apply(analysis, digest)
    with store.GraphStore(_store_path(index)) as db:
        for position, analysis, _, lang, data in parsed:
            result = _result(index, analysis)
            _write_file(index, analysis.path, lang, data, analysis, result, db=db)
            results[position] = result
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
    db_path = str(project_paths.graph_db_path(index.root))
    if os.path.isfile(db_path):
        with store.GraphStore(db_path) as db:
            db.delete_path_subtree(rel_path)
    return {"ok": True, "op": "remove_file", "path": rel_path}


def _root(req: dict) -> str:
    root = req.get("root")
    if not isinstance(root, str) or not root.strip():
        raise ProtocolError("bad_request", "root is required")
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise ProtocolError("root_not_found", f"not a directory: {root}")
    return root


def _sync(req: dict) -> dict:
    root = _root(req)
    result = project_scan.sync(root, ENGINE_VERSION, SCHEMA_VERSION)
    return {"ok": True, "op": "sync", **result}


def _status(req: dict) -> dict:
    root = _root(req)
    db_path = str(project_paths.graph_db_path(root))
    if not os.path.isfile(db_path):
        return {
            "ok": True,
            "op": "status",
            "up_to_date": False,
            "db_path": db_path,
            "stats": {
                "nodes": 0,
                "edges": 0,
                "stale": 0,
                "files": 0,
                "symbols": 0,
                "unresolved": 0,
            },
        }
    with store.GraphStore(db_path, readonly=True) as db:
        return {
            "ok": True,
            "op": "status",
            "up_to_date": db.is_up_to_date(root, ENGINE_VERSION, SCHEMA_VERSION),
            "db_path": db_path,
            "stats": db.stats(),
            "meta": db.meta(),
        }


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
    if op == "sync":
        return _sync(req)
    if op == "status":
        return _status(req)
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
