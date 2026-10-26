#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prompt template parsing/rendering without app-specific path or i18n policy."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_KEY_RE = re.compile(r"^([a-zA-Z_]+)\s*:\s*(.*)$")
_LIST_RE = re.compile(r"^\[(.*)\]$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class PromptLoadError(RuntimeError):
    """Template unavailable or contract violation."""


@dataclass(frozen=True)
class PromptTemplate:
    feature: str
    lang: str
    path: str
    text: str
    placeholders: tuple[str, ...]
    json_keys: tuple[str, ...]


def _parse_list(raw: str) -> tuple[str, ...]:
    m = _LIST_RE.match(raw.strip())
    if not m:
        return ()
    items = [item.strip().strip("\"'") for item in m.group(1).split(",")]
    return tuple(item for item in items if item)


def parse_template(raw: str, feature: str, path: Path | str) -> PromptTemplate:
    """Parse and self-check a template text."""
    path = Path(path)
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise PromptLoadError(
            f"{path}: missing front-matter (--- name/placeholders/json_keys ---)"
        )
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        km = _KEY_RE.match(line.strip())
        if not km:
            raise PromptLoadError(f"{path}: cannot parse front-matter line {line!r}")
        meta[km.group(1)] = km.group(2)

    name = meta.get("name", "").strip()
    if name != feature:
        raise PromptLoadError(
            f"{path}: name={name!r} does not match the requested feature={feature!r}"
        )

    text = raw[m.end() :]
    declared = _parse_list(meta.get("placeholders", ""))
    found = tuple(dict.fromkeys(_PLACEHOLDER_RE.findall(text)))
    if set(declared) != set(found):
        raise PromptLoadError(
            f"{path}: placeholders declaration does not match actual "
            f"(declared {sorted(declared)} / actual {sorted(found)})"
        )
    return PromptTemplate(
        feature=feature,
        lang=path.parent.name,
        path=str(path),
        text=text,
        placeholders=declared,
        json_keys=_parse_list(meta.get("json_keys", "")),
    )


def render(tpl: PromptTemplate, variables: dict) -> str:
    """One-shot replacement of declared placeholders by name."""
    missing = [p for p in tpl.placeholders if p not in variables]
    if missing:
        raise PromptLoadError(
            f"{tpl.feature}: call missing argument {missing} "
            f"(declared {list(tpl.placeholders)})"
        )
    extra = [k for k in variables if k not in tpl.placeholders]
    if extra:
        logger.warning("%s: call passes extra argument %s", tpl.feature, extra)

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(variables[name]) if name in variables else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, tpl.text)
