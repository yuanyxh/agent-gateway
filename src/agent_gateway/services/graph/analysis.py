"""In-memory symbol table shared by the indexer and the scanner."""

from __future__ import annotations

import hashlib
import json
import os

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.graph.model import FileAnalysis, ParseError
from agent_gateway.services.graph.parsers import EXTENSIONS, PARSERS

logger = setup_logging("graph", json_format=True)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".build",
        ".gradle",
        ".next",
        ".nuxt",
        ".agent-gateway",
        ".utility",
        ".venv",
        "DerivedData",
        "Pods",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "Utility.app",
    }
)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def walk_files(root: str):
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
            self._load(rel)
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
        for full in walk_files(self.root):
            rel = os.path.relpath(full, self.root).replace(os.sep, "/")
            self._load(rel)
        self._loaded = True

    def _load(self, rel: str) -> None:
        lang = EXTENSIONS.get(os.path.splitext(rel)[1].lower())
        if lang is None or lang not in PARSERS:
            return
        try:
            with open(os.path.join(self.root, rel), "rb") as handle:
                data = handle.read()
            analysis = PARSERS[lang](data, rel)
        except Exception as exc:  # noqa: BLE001 - one file must not abort the scan
            self.failed_files.append(rel)
            logger.warning(
                json.dumps({"event": "index_failed", "file": rel, "error": str(exc)})
            )
            return
        self.analyses[rel] = analysis
        self._hashes[rel] = sha1(data)
        self._langs[rel] = lang
        self._add_symbols(analysis)

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

    def content_hash(self, path: str) -> str | None:
        return self._hashes.get(path)

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
        digest = sha1(data)
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


__all__ = ["ProjectIndex", "SKIP_DIRS", "sha1", "walk_files", "ParseError"]
