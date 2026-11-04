"""Name binding for graph references.

Layers, in order: same file, import scoped, module scoped, globally unique.
Candidates that survive no layer are reported as unresolved; they never
produce an edge.
"""

from __future__ import annotations

import posixpath
from typing import Iterable, Protocol

from agent_gateway.services.graph.constants import (
    CALLABLE_KINDS,
    JS_EXTENSIONS,
    LANG_FAMILY,
    TYPE_KINDS,
)
from agent_gateway.services.graph.model import (
    FileAnalysis,
    ImportRef,
    Reference,
    SymbolDef,
)

RESOLVER_CONFIDENCE = {
    "local": 1.0,
    "import_scoped": 0.9,
    "re_export": 0.85,
    "commonjs_export": 0.8,
    "module_scoped": 0.8,
    "global_unique": 0.6,
    "import_path": 1.0,
}


class SymbolIndex(Protocol):
    @property
    def by_name(self) -> dict[str, list[SymbolDef]]: ...

    @property
    def file_paths(self) -> set[str]: ...

    @property
    def analyses(self) -> dict[str, FileAnalysis]: ...


class PathIndex:
    """Suffix lookup for project paths, built once per resolve pass."""

    def __init__(self, paths: set[str]) -> None:
        self.paths = sorted(paths)
        self._exact = set(paths)
        self._suffix: dict[str, list[str]] = {}
        for path in self.paths:
            parts = path.split("/")
            for start in range(len(parts)):
                key = "/".join(parts[start:])
                self._suffix.setdefault(key, []).append(path)

    def has(self, path: str) -> bool:
        return path in self._exact

    def suffix(self, suffix: str) -> list[str]:
        return self._suffix.get(suffix, [])


def _family(path: str) -> str:
    return LANG_FAMILY.get(posixpath.splitext(path)[1].lower(), "")


def _module_path(receiver: str, paths: PathIndex, current_path: str = "") -> str | None:
    """Map a module receiver to one project file, if unambiguous."""
    if current_path.endswith(JS_EXTENSIONS):
        base = posixpath.dirname(current_path)
        for ext in JS_EXTENSIONS:
            for candidate in (
                posixpath.join(base, receiver + ext),
                posixpath.join(base, receiver, "index" + ext),
            ):
                candidate = posixpath.normpath(candidate)
                if paths.has(candidate):
                    return candidate
        return None
    parts = receiver.split(".")
    for size in (len(parts), 1):
        suffix = "/".join(parts[-size:]) + ".py"
        hits = paths.suffix(suffix)
        if len(hits) == 1:
            return hits[0]
    return None


def _re_export_source(
    module_path: str, name: str, index: SymbolIndex, paths: PathIndex
) -> SymbolDef | None:
    """Follow `from a import name` inside the receiver module to the definition."""
    analysis = index.analyses.get(module_path)
    if analysis is None:
        return None
    for imported in analysis.imports:
        target_name = name
        for local, original in imported.aliases:
            if local == name and original != "*":
                target_name = original
                break
        if target_name not in imported.names:
            continue
        for target in sorted(import_paths(module_path, imported, paths)):
            hits = [
                item
                for item in index.by_name.get(target_name, ())
                if item.path == target
            ]
            if hits:
                return sorted(hits, key=lambda item: item.start_line)[0]
    return None


def _kind_matches(reference_kind: str, symbol_kind: str) -> bool:
    if reference_kind in ("extends", "type_ref"):
        return symbol_kind in TYPE_KINDS or symbol_kind == "typealias"
    if reference_kind in ("call", "override"):
        return symbol_kind in CALLABLE_KINDS
    return True


def _kind_filter(candidates: list[SymbolDef], reference_kind: str) -> list[SymbolDef]:
    """Drop candidates whose kind cannot satisfy this reference."""
    if reference_kind in ("call", "override"):
        return [item for item in candidates if item.kind in CALLABLE_KINDS]
    if reference_kind in ("extends", "type_ref"):
        return [
            item
            for item in candidates
            if item.kind in TYPE_KINDS or item.kind == "typealias"
        ]
    return candidates


def _choose(candidates: Iterable[SymbolDef], reference_kind: str) -> SymbolDef:
    pool = list(candidates)
    matching = [item for item in pool if _kind_matches(reference_kind, item.kind)]
    if matching:
        pool = matching
    return sorted(pool, key=lambda item: (item.path, item.start_line, item.name))[0]


def _scoped_choice(
    candidates: list[SymbolDef], reference_kind: str
) -> SymbolDef | None:
    """A scope that still spans several files cannot pick a binding."""
    if len({item.path for item in candidates}) > 1:
        return None
    return _choose(candidates, reference_kind)


def _module_file_matches(module_path: str, paths: PathIndex) -> list[str]:
    module_path = posixpath.normpath(module_path)
    if module_path.startswith("./"):
        module_path = module_path[2:]
    found: list[str] = []
    for candidate in (module_path + ".py", posixpath.join(module_path, "__init__.py")):
        candidate = posixpath.normpath(candidate)
        if paths.has(candidate):
            found.append(candidate)
            continue
        matches = paths.suffix(candidate)
        if len(matches) == 1:
            found.append(matches[0])
    return found


JS_ALIASES = ("@/", "~/")


def _js_tail_matches(tail: str, paths: PathIndex) -> list[str]:
    norm = tail.lstrip("/")
    if norm.endswith(JS_EXTENSIONS):
        hits = list(paths.suffix(norm))
        if paths.has(norm):
            hits.append(norm)
        if hits:
            return hits
    for ext in JS_EXTENSIONS:
        for suffix in (norm + ext, norm + "/index" + ext):
            hits = list(paths.suffix(suffix))
            if paths.has(suffix):
                hits.append(suffix)
            if hits:
                return hits
    return []


def _js_module_matches(current_path: str, module: str, paths: PathIndex) -> list[str]:
    if module.startswith("."):
        base = posixpath.normpath(
            posixpath.join(posixpath.dirname(current_path), module)
        )
        if paths.has(base) and base.endswith(JS_EXTENSIONS):
            return [base]
        candidates = [base + ext for ext in JS_EXTENSIONS]
        candidates += [posixpath.join(base, "index" + ext) for ext in JS_EXTENSIONS]
        return [path for path in candidates if paths.has(path)]
    for alias in JS_ALIASES:
        if module.startswith(alias):
            return _js_tail_matches(module[len(alias) :], paths)
    return []


def _package_matches(
    dotted: str, paths: PathIndex, extensions: tuple[str, ...]
) -> list[str]:
    """Map a Java/Kotlin package import to project files by path suffix."""
    if dotted.endswith(".*"):
        prefix = "/" + dotted[:-2].replace(".", "/") + "/"
        return [
            path
            for path in paths.paths
            if path.endswith(extensions) and prefix in "/" + path
        ]
    probes = [dotted]
    parts = dotted.split(".")
    if len(parts) > 2:
        probes.append(".".join(parts[:-1]))
    for probe in probes:
        suffix = "/" + probe.replace(".", "/")
        hits = [
            path
            for path in paths.paths
            if any(path.endswith(suffix + ext) for ext in extensions)
        ]
        if hits:
            return hits
    return []


def import_paths(current_path: str, imported: ImportRef, paths: PathIndex) -> set[str]:
    """Map one import to project-relative Python files (Swift modules map to none)."""
    found: set[str] = set()
    if imported.level:
        base = posixpath.dirname(current_path)
        for _ in range(imported.level - 1):
            base = posixpath.dirname(base)
        module_path = (
            posixpath.join(base, imported.module.replace(".", "/"))
            if imported.module
            else base
        )
    else:
        if not imported.module:
            return found
        module_path = imported.module.replace(".", "/")

    if current_path.endswith(".py"):
        found.update(_module_file_matches(module_path, paths))
        for name in imported.names:
            found.update(_module_file_matches(posixpath.join(module_path, name), paths))
    elif current_path.endswith(JS_EXTENSIONS):
        found.update(_js_module_matches(current_path, imported.module, paths))
    elif current_path.endswith(".java"):
        found.update(_package_matches(imported.module, paths, (".java",)))
    elif current_path.endswith((".kt", ".kts")):
        found.update(_package_matches(imported.module, paths, (".kt", ".kts")))
    return found


def _module_root(path: str) -> str:
    """Swift module root: Sources/<Target> when present, else the top folder."""
    parts = path.replace("\\", "/").split("/")
    if "Sources" in parts:
        index = parts.index("Sources")
        if index + 1 < len(parts) and not posixpath.splitext(parts[index + 1])[1]:
            return "/".join(parts[: index + 2])
        return "/".join(parts[: index + 1])
    return parts[0] if len(parts) > 1 else "."


def _type_scope(analysis: FileAnalysis, line: int) -> SymbolDef | None:
    """Innermost type declaration containing `line` (self/this binding)."""
    best: SymbolDef | None = None
    for symbol in analysis.symbols:
        if symbol.kind not in TYPE_KINDS:
            continue
        if not symbol.start_line <= line <= symbol.end_line:
            continue
        if best is None or (symbol.end_line - symbol.start_line) <= (
            best.end_line - best.start_line
        ):
            best = symbol
    return best


def _receiver_module_paths(
    analysis: FileAnalysis, receiver: str, paths: PathIndex
) -> set[str]:
    """Files reachable through a module import named or prefixed by `receiver`."""
    found: set[str] = set()
    for imported in analysis.imports:
        if imported.level or not imported.module:
            continue
        aliased = any(
            local == receiver and original != "*"
            for local, original in imported.aliases
        )
        if not (
            aliased
            or receiver in imported.names
            or imported.module == receiver
            or imported.module.startswith(receiver + ".")
        ):
            continue
        found |= import_paths(analysis.path, imported, paths)
    return found


def _import_edges(
    analysis: FileAnalysis, index: SymbolIndex, paths: PathIndex
) -> tuple[list[dict], set[str]]:
    edges: list[dict] = []
    imported_paths: set[str] = set()
    seen: set[tuple[str, int, str]] = set()
    for imported in analysis.imports:
        targets = import_paths(analysis.path, imported, paths)
        imported_paths |= targets
        raw_name = imported.module or ".".join(imported.names) or "." * imported.level
        for target in sorted(targets):
            key = (target, imported.line, raw_name)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "from_name": None,
                    "from_line": None,
                    "raw_name": raw_name,
                    "kind": "import",
                    "line": imported.line,
                    "confidence": RESOLVER_CONFIDENCE["import_path"],
                    "resolver": "import_path",
                    "to_path": target,
                    "to_name": None,
                    "to_kind": None,
                    "to_signature": None,
                }
            )
    imported_paths.discard(analysis.path)
    return edges, imported_paths


def _edge(
    reference: Reference,
    target: SymbolDef,
    resolver: str,
) -> dict:
    kind = reference.kind
    if kind == "extends" and target.kind in ("protocol", "interface"):
        kind = "implements"
    return {
        "from_name": reference.from_name,
        "from_line": reference.from_line,
        "raw_name": reference.raw_name,
        "kind": kind,
        "line": reference.line,
        "confidence": RESOLVER_CONFIDENCE[resolver],
        "resolver": resolver,
        "to_path": target.path,
        "to_name": target.name,
        "to_kind": target.kind,
        "to_signature": target.signature,
        "to_start_line": target.start_line,
    }


def _unresolved(reference: Reference, reason: str) -> dict:
    return {
        "raw_name": reference.raw_name,
        "kind": reference.kind,
        "line": reference.line,
        "reason": reason,
    }


def _require_alias_target(
    analysis: FileAnalysis, name: str, index: SymbolIndex, paths: PathIndex
) -> SymbolDef | None:
    """Resolve `const x = require("./m"); x()` to the module's single export."""
    for imported in analysis.imports:
        if imported.kind != "require" or name not in imported.names:
            continue
        for target in sorted(import_paths(analysis.path, imported, paths)):
            exported = index.exports.get(target, [])
            if len(exported) != 1:
                continue
            hits = [
                item
                for item in index.by_name.get(exported[0], ())
                if item.path == target
            ]
            if len(hits) == 1:
                return hits[0]
    return None


def resolve_file(
    analysis: FileAnalysis, index: SymbolIndex
) -> tuple[list[dict], list[dict]]:
    """Resolve one file's references against the project symbol table."""
    paths = PathIndex(index.file_paths)
    import_edges, imported_paths = _import_edges(analysis, index, paths)
    edges = list(import_edges)
    unresolved: list[dict] = []

    aliases: dict[str, str] = {}
    namespace_imports: dict[str, ImportRef] = {}
    for imported in analysis.imports:
        for local, original in imported.aliases:
            if original == "*":
                namespace_imports[local] = imported
            else:
                aliases[local] = original

    seen_refs: set[tuple[str, str, int, str | None]] = set()
    for reference in analysis.references:
        # Exports are bookkeeping for commonjs_export, not caller evidence.
        if reference.kind == "export":
            continue
        ref_key = (
            reference.raw_name,
            reference.kind,
            reference.line,
            reference.from_name,
            reference.receiver,
        )
        if ref_key in seen_refs:
            continue
        seen_refs.add(ref_key)

        lookup_name = aliases.get(reference.raw_name, reference.raw_name)
        candidates = _kind_filter(
            list(index.by_name.get(lookup_name, ())), reference.kind
        )

        receiver = reference.receiver
        if receiver and receiver not in ("self", "super", "this", "cls"):
            namespace = namespace_imports.get(receiver)
            namespace_paths: set[str] = set()
            if namespace is not None:
                namespace_paths |= import_paths(analysis.path, namespace, paths)
            namespace_paths |= _receiver_module_paths(analysis, receiver, paths)
            if namespace_paths:
                scoped = [item for item in candidates if item.path in namespace_paths]
                if scoped:
                    edges.append(
                        _edge(
                            reference,
                            _choose(scoped, reference.kind),
                            "import_scoped",
                        )
                    )
                    continue
                if len(namespace_paths) == 1:
                    exported = _re_export_source(
                        next(iter(namespace_paths)), lookup_name, index, paths
                    )
                    if exported is not None:
                        edges.append(_edge(reference, exported, "re_export"))
                        continue
            type_candidates = [
                item
                for item in index.by_name.get(receiver, ())
                if item.kind in TYPE_KINDS
            ]
            type_paths = {item.path for item in type_candidates}
            scoped = [item for item in candidates if item.path in type_paths]
            if scoped:
                edges.append(
                    _edge(reference, _choose(scoped, reference.kind), "module_scoped")
                )
                continue
            module_path = _module_path(receiver, paths, analysis.path)
            if module_path is not None:
                direct = [item for item in candidates if item.path == module_path]
                if direct:
                    edges.append(
                        _edge(
                            reference, _choose(direct, reference.kind), "module_scoped"
                        )
                    )
                    continue
                exported = _re_export_source(module_path, lookup_name, index, paths)
                if exported is not None:
                    edges.append(_edge(reference, exported, "re_export"))
                    continue
            unresolved.append(_unresolved(reference, "no_binding"))
            continue

        local_pool = analysis.symbols
        if receiver in ("self", "super", "this", "cls"):
            scope = _type_scope(analysis, reference.line)
            if scope is not None:
                scoped_pool = [
                    symbol
                    for symbol in analysis.symbols
                    if scope.start_line <= symbol.start_line
                    and symbol.end_line <= scope.end_line
                ]
                if any(symbol.name == reference.raw_name for symbol in scoped_pool):
                    local_pool = scoped_pool
        local_matches = _kind_filter(
            [symbol for symbol in local_pool if symbol.name == reference.raw_name],
            reference.kind,
        )
        if local_matches:
            edges.append(
                _edge(reference, _choose(local_matches, reference.kind), "local")
            )
            continue

        alias_target = _require_alias_target(analysis, reference.raw_name, index, paths)
        if alias_target is not None:
            edges.append(_edge(reference, alias_target, "commonjs_export"))
            continue

        imported_candidates = [
            item
            for item in candidates
            if item.path in imported_paths and item.path != analysis.path
        ]
        if imported_candidates:
            choice = _scoped_choice(imported_candidates, reference.kind)
            if choice is not None:
                edges.append(_edge(reference, choice, "import_scoped"))
            else:
                unresolved.append(_unresolved(reference, "ambiguous"))
            continue

        current_dir = posixpath.dirname(analysis.path)
        if analysis.path.endswith(".swift"):
            root = _module_root(analysis.path)
            module_candidates = [
                item
                for item in candidates
                if item.path != analysis.path and _module_root(item.path) == root
            ]
        else:
            module_candidates = [
                item
                for item in candidates
                if posixpath.dirname(item.path) == current_dir
                and item.path != analysis.path
            ]
        if module_candidates:
            choice = _scoped_choice(module_candidates, reference.kind)
            if choice is not None:
                edges.append(_edge(reference, choice, "module_scoped"))
            else:
                unresolved.append(_unresolved(reference, "ambiguous"))
            continue

        if len(candidates) == 1:
            candidate = candidates[0]
            family = _family(candidate.path)
            if family in ("", _family(analysis.path)):
                edges.append(_edge(reference, candidate, "global_unique"))
                continue
            unresolved.append(_unresolved(reference, "no_binding"))
            continue

        reason = "ambiguous" if candidates else "no_binding"
        unresolved.append(_unresolved(reference, reason))

    edges.sort(
        key=lambda item: (
            item["line"] or 0,
            item["from_name"] or "",
            item["raw_name"],
            item["kind"],
            item["to_path"],
        )
    )
    unresolved.sort(
        key=lambda item: (item["line"] or 0, item["raw_name"], item["kind"])
    )
    return edges, unresolved
