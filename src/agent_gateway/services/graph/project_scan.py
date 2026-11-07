"""Scan a project and write its graph through the gateway-owned store."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_gateway.core import config_loader, project_paths
from agent_gateway.services.graph import resolver, store
from agent_gateway.services.graph.analysis import ProjectIndex, sha1, walk_files
from agent_gateway.services.graph.constants import EXTENSIONS
from agent_gateway.services.graph.model import FileAnalysis
from agent_gateway.services.graph.parsers import PARSERS

try:
    import pathspec
except ImportError:  # pragma: no cover - declared dependency
    pathspec = None


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    full: str
    lang: str
    size: int
    mtime: float


def _config(root: str) -> dict:
    target = project_paths.project_state_dir(root) / "config.json"
    legacy = os.path.join(root, ".utility", "config.json")
    for path in (target, legacy):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _spec(patterns: object):
    if not isinstance(patterns, list) or not patterns or pathspec is None:
        return None
    lines = [str(item) for item in patterns if isinstance(item, str)]
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _discover(root: str) -> dict[str, DiscoveredFile]:
    config = _config(root)
    includes = _spec(config.get("includes"))
    excludes = _spec(config.get("excludes"))
    allowed = config.get("languages")
    if not isinstance(allowed, list) or not allowed:
        allowed = config_loader.value("graph.languages", [])
    allowed_langs = (
        {item.lower() for item in allowed if isinstance(item, str)}
        if isinstance(allowed, list)
        else set()
    )
    found: dict[str, DiscoveredFile] = {}
    for full in walk_files(root):
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if includes is not None and not includes.match_file(rel):
            continue
        if excludes is not None and excludes.match_file(rel):
            continue
        lang = EXTENSIONS.get(os.path.splitext(rel)[1].lower())
        if lang is None or lang not in PARSERS:
            continue
        if allowed_langs and lang.lower() not in allowed_langs:
            continue
        try:
            stat = os.stat(full)
        except OSError:
            continue
        found[rel] = DiscoveredFile(
            path=rel, full=full, lang=lang, size=stat.st_size, mtime=stat.st_mtime
        )
    return found


def _source_key(analysis: FileAnalysis, edge: dict) -> str:
    path = analysis.path
    name = edge.get("from_name")
    if not name:
        return store.file_key(path)
    line = edge.get("from_line")
    candidates = [item for item in analysis.symbols if item.name == name]
    if line is not None:
        for item in candidates:
            if item.start_line <= line <= item.end_line:
                return store.symbol_key(
                    store.sym_key(
                        path, item.name, item.kind, item.signature, item.start_line
                    )
                )
    if candidates:
        item = candidates[0]
        return store.symbol_key(
            store.sym_key(path, item.name, item.kind, item.signature, item.start_line)
        )
    return store.file_key(path)


def _target_key(edge: dict) -> str | None:
    path = edge.get("to_path")
    if not path:
        return None
    name = edge.get("to_name")
    if name is None:
        return store.file_key(path)
    line = edge.get("to_start_line")
    if line is None:
        return None
    return store.symbol_key(
        store.sym_key(
            path,
            name,
            str(edge.get("to_kind") or ""),
            edge.get("to_signature"),
            int(line),
        )
    )


def _edge_rows(analysis: FileAnalysis, edges: list[dict]) -> list[store.EdgeRow]:
    rows: list[store.EdgeRow] = []
    for edge in edges:
        to_key = _target_key(edge)
        if to_key is None:
            continue
        line = edge.get("line")
        if line is None:
            line = edge.get("from_line")
        rows.append(
            store.EdgeRow(
                from_key=_source_key(analysis, edge),
                to_key=to_key,
                kind=str(edge.get("kind") or ""),
                path=analysis.path,
                start_line=line,
                end_line=line,
                confidence=float(edge.get("confidence") or 0.0),
                resolver=str(edge.get("resolver") or ""),
            )
        )
    return rows


def _unresolved_rows(
    analysis: FileAnalysis, unresolved: list[dict]
) -> list[store.UnresolvedRow]:
    return [
        store.UnresolvedRow(
            path=analysis.path,
            raw_name=str(item.get("raw_name") or ""),
            kind=str(item.get("kind") or ""),
            line=item.get("line"),
            reason=str(item.get("reason") or ""),
        )
        for item in unresolved
    ]


def _parse(
    index: ProjectIndex, discovered: dict[str, DiscoveredFile]
) -> list[FileAnalysis]:
    analyses: list[FileAnalysis] = []
    for rel in sorted(discovered):
        item = discovered[rel]
        try:
            with open(item.full, "rb") as handle:
                data = handle.read()
            analysis = PARSERS[item.lang](data, rel)
        except Exception:  # noqa: BLE001 - one file must not abort the scan
            index.failed_files.append(rel)
            continue
        index.analyses[rel] = analysis
        index._hashes[rel] = sha1(data)
        index._langs[rel] = item.lang
        index._add_symbols(analysis)
        analyses.append(analysis)
    index._loaded = True
    return analyses


def _symbol_rows(analyses: list[FileAnalysis]) -> list[store.SymbolRow]:
    rows: list[store.SymbolRow] = []
    for analysis in analyses:
        for symbol in analysis.symbols:
            rows.append(
                store.SymbolRow(
                    sym_key=store.sym_key(
                        analysis.path,
                        symbol.name,
                        symbol.kind,
                        symbol.signature,
                        symbol.start_line,
                    ),
                    path=analysis.path,
                    name=symbol.name,
                    kind=symbol.kind,
                    signature=symbol.signature,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                )
            )
    return rows


def _file_rows(
    discovered: dict[str, DiscoveredFile], index: ProjectIndex
) -> list[store.FileRow]:
    rows: list[store.FileRow] = []
    for item in discovered.values():
        digest = index.content_hash(item.path)
        if digest is None:
            try:
                with open(item.full, "rb") as handle:
                    digest = sha1(handle.read())
            except OSError:
                continue
        rows.append(
            store.FileRow(
                path=item.path,
                lang=item.lang,
                size=item.size,
                mtime=item.mtime,
                content_hash=digest,
            )
        )
    return rows


def sync(root: str, engine: str, schema: str) -> dict:
    """Reconcile the project graph; a changed project is rebuilt atomically."""
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    started = time.perf_counter()
    db_path = project_paths.graph_db_path(root)
    project_paths.copy_legacy(project_paths.legacy_graph_db_path(root), db_path)
    db = store.GraphStore(str(db_path))
    try:
        discovered = _discover(root)
        known = db.all_file_meta()
        if db.is_up_to_date(root, engine, schema):
            changed = False
            for rel, item in discovered.items():
                meta = known.get(rel)
                if (
                    meta is not None
                    and meta[0] == item.size
                    and abs(meta[1] - item.mtime) < 0.001
                ):
                    continue
                changed = True
                break
            removed = set(known) - set(discovered)
            if not changed and not removed:
                return {"ok": True, "stats": db.stats(), "db_path": str(db_path)}

        index = ProjectIndex(root)
        analyses = _parse(index, discovered)
        symbols = _symbol_rows(analyses)
        files = _file_rows(discovered, index)
        edges: list[store.EdgeRow] = []
        unresolved: list[store.UnresolvedRow] = []
        for analysis in analyses:
            raw_edges, raw_unresolved = resolver.resolve_file(analysis, index)
            edges.extend(_edge_rows(analysis, raw_edges))
            unresolved.extend(_unresolved_rows(analysis, raw_unresolved))

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        meta = {
            "engine_version": engine,
            "schema_version": schema,
            "root": root,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "full_build_elapsed_ms": str(elapsed_ms),
        }
        partial_files = sorted(
            analysis.path for analysis in analyses if analysis.partial_parse
        )
        diagnostics = {
            "engine_version": engine,
            "schema_version": schema,
            "last_full_elapsed_ms": str(elapsed_ms),
            "failed_files": json.dumps(index.failed_files[:50], ensure_ascii=False),
            # Parsed with error nodes: symbols exist but may be incomplete.
            "partial_parse_files": json.dumps(partial_files[:50], ensure_ascii=False),
            "unresolved_top": json.dumps(
                [
                    f"{item.path}:{item.line or 0} {item.raw_name} {item.kind} {item.reason}"
                    for item in unresolved[:20]
                ],
                ensure_ascii=False,
            ),
        }
        db.replace_graph(meta, files, symbols, edges, unresolved, diagnostics)
        return {
            "ok": True,
            "stats": db.stats(),
            "db_path": str(db_path),
            "elapsed_ms": elapsed_ms,
            "failed_files": len(index.failed_files),
            "partial_parse_files": len(partial_files),
        }
    finally:
        db.close()
