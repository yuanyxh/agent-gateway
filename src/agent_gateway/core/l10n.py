"""Shared i18n runtime for the Python side.

The catalog is generated from `Sources/Resources/<lang>.json` (the same files the Swift app
uses). Keys are namespaced; only `mcp.*` and `proxy.*` reach this runtime. The language comes
from `config.ui_language()` ← `UTILITY_LANG` ← Swift `general.language`, and is normalised by
the same rule as `L10n.resolvedLanguage` so both sides agree on "which language is active".

Placeholders use the shared grammar: `{name}` (string), `{name:d}` (int), `{name:.Nf}` (double).
Replacement is a single pass — a substituted value containing `{other}` is not expanded again.
"""

from __future__ import annotations

import re

from agent_gateway.core import l10n_catalog
from agent_gateway.core.config import ui_language
from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("l10n")

BASELINE_LANG = "zh-Hans"

AVAILABLE_LANGUAGES: tuple[str, ...] = tuple(l10n_catalog.AVAILABLE_LANGUAGES)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::(\.\d+f|d))?\}")

#: Compact-number units per language. The first matching threshold wins.
_COMPACT_RULES: dict[str, tuple[tuple[int, str, int], ...]] = {
    "zh-Hans": ((10_000, " 万", 1),),
    "en": ((1_000, "K", 0),),
}
_DEFAULT_COMPACT = ((1_000, "K", 0),)

_warned: set[str] = set()


class MissingText(KeyError):
    """A key or placeholder value that does not exist. Fail loud: silently rendering `ns.key`
    would send an internal identifier to the user or the model."""


def normalize(language: str) -> str:
    """Exact match → baseline family (`zh-*` → `zh-Hans`) → English → baseline."""
    raw = (language or "").replace("_", "-").lower()
    if not raw:
        return BASELINE_LANG
    for lang in AVAILABLE_LANGUAGES:
        if lang.lower() == raw:
            return lang
    family = raw.split("-", 1)[0]
    if (
        family == BASELINE_LANG.split("-", 1)[0]
        and BASELINE_LANG in AVAILABLE_LANGUAGES
    ):
        return BASELINE_LANG
    for lang in AVAILABLE_LANGUAGES:
        if lang.lower() == "en":
            return lang
    return BASELINE_LANG


def resolved_language(configured: str = "") -> str:
    return normalize(configured.strip() or ui_language().strip())


def t(key: str, *, lang: str | None = None, **values) -> str:
    """Render `key` in the given (or current) language, falling back to the baseline per key."""
    entry = l10n_catalog.CATALOG.get(key)
    if entry is None:
        raise MissingText(f"unregistered text key: {key!r}")
    language = normalize(lang) if lang else resolved_language()
    template = entry.get(language)
    if template is None:
        template = entry.get(BASELINE_LANG)
        if template is None:
            raise MissingText(
                f"{key!r} has no text for {language!r} or {BASELINE_LANG!r}"
            )
        if language != BASELINE_LANG and key not in _warned:
            _warned.add(key)
            logger.warning(
                f"{key}: no {language!r} text, falling back to {BASELINE_LANG!r}"
            )
    return _render(template, values, key)


def all_templates(key: str) -> dict[str, str]:
    """Every language's raw template for `key` (used to derive text that must be recognisable
    regardless of the language that produced it)."""
    entry = l10n_catalog.CATALOG.get(key)
    if entry is None:
        raise MissingText(f"unregistered text key: {key!r}")
    return dict(entry)


def compact_number(n: int, lang: str) -> str:
    """Compact magnitude with the language's unit (`12.3 万` / `12K`)."""
    for threshold, suffix, digits in _COMPACT_RULES.get(lang, _DEFAULT_COMPACT):
        if n >= threshold:
            return f"{n / threshold:.{digits}f}{suffix}"
    return str(n)


def _render(template: str, values: dict[str, object], key: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, spec = match.group(1), match.group(2)
        if name not in values:
            raise MissingText(f"{key!r}: no value for placeholder {name!r}")
        value = values[name]
        if spec == "d":
            return str(int(value))
        if spec:
            return format(float(value), spec)
        return str(value)

    return _PLACEHOLDER_RE.sub(replace, template)
