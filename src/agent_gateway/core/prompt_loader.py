"""Unified entry point for Python-side prompts: built-in defaults + user overrides + declarative contract.

Prompts used to be hard-coded across 10 functions/constants (f-strings or long constants),
which had three consequences: changing a single prompt sentence required digging through the
source; the user is not necessarily a Chinese speaker; and a mistake neither raised an error nor
fell back. This module collapses "get a prompt" into one thing.

Directories and priority (for the same feature, only the highest-priority file is used):

    <config-dir>/prompts/<feature>.md              user override, highest priority, **language-independent**
    <MCPScripts>/prompts/<lang>/<feature>.md       built-in default, shipped with the package (.copy preserves the dir tree)

The language dimension of the two locations is **intentionally different**: built-in templates
target "users who may speak different native languages", so they are split into language dirs and
fall back to the baseline when a language is missing; a user override is a single file that **the
user controls**——the person writing the prompt has only one native language, so making "which
directory it goes in" depend on the UI language is both meaningless and would break when the
language is switched.

The config dir reuses the **existing** env contract: the `UTILITY_DATA_DIR` injected by Swift is
`~/.config/utility` (`AppPaths.configDir`), so no new env item is added.

The language comes from `UTILITY_LANG` (Swift sets it from `general.language`, empty falls back to
the system primary); the default is `zh-Hans`; when the language has no such file it **falls back
to the `zh-Hans` baseline** and logs a warning——a not-yet-translated feature should not stall the
feature, but it must not be silent either (otherwise "the translation didn't take effect" would be
impossible to discover).

Declarative contract (front-matter, flat keys, no YAML dependency):

    ---
    name: aggregate
    placeholders: [label, facts_text]
    json_keys: []
    ---

- `name` must match the feature name: a misplaced file (e.g. writing a.md's content into b.md)
  is exposed immediately, instead of silently using the wrong prompt.
- `placeholders` must match the placeholders actually appearing in the body (file self-check);
  at call time the arguments must cover the declaration (missing → raise `PromptLoadError`,
  otherwise `{topic}` would be sent to the model verbatim); extra arguments only log a warning.
- `json_keys` are the **keys expected in the model output**, for post-call validation
  (`missing_json_keys`); empty = not declared.

Rendering does **not use `str.format`**: prompts contain many JSON braces (`{"action": "merge"}`),
and format would treat them as placeholders. Instead it uses "one-shot replacement by name"——
unregistered `{...}` are kept verbatim, and substituted values are not replaced a second time
(otherwise a value containing `{x}` would be clobbered by a later replacement).
This is the same pattern as Swift `PromptManager.applyVariables`.

Failure-handling chain during load: an invalid user override file → error log + **keep looking for
the built-in default** (a user's mistake should not stall the feature); the built-in default is also
unreadable → raise `PromptLoadError` (a corrupt install is a development-time error; silently
passing an empty prompt to the model is worse than failing outright).
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

from agent_gateway.core.config import data_dir
from agent_gateway.core.l10n import BASELINE_LANG, resolved_language
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.prompt_template import (
    PromptLoadError,
    PromptTemplate,
    parse_template,
    render,
)

logger = setup_logging("prompt_loader")

#: Baseline language. Any language missing this feature falls back to it; the built-in defaults
#: must guarantee this language is complete.
FALLBACK_LANG = BASELINE_LANG

#: Characters allowed in a feature name (used for filename concatenation, guards against directory traversal)
_FEATURE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_cache: dict[str, PromptTemplate] = {}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _builtin_root() -> Path:
    return Path(str(files("agent_gateway") / "prompts"))


def _override_root() -> Path | None:
    """User override root directory (config dir). None when `UTILITY_DATA_DIR` is not injected."""
    base = data_dir()
    return Path(base) / "prompts" if base else None


def current_lang() -> str:
    return resolved_language()


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def _parse_file(path: Path, feature: str) -> PromptTemplate:
    """Parse and self-check a template file; any contract violation raises `PromptLoadError`."""
    return parse_template(path.read_text(encoding="utf-8"), feature, path)


def _candidate_paths(feature: str) -> list[tuple[Path, str | None]]:
    """Return candidate files by priority: (path, lang); a lang of `None` means **language-independent**.

    User overrides (`_override_root()`) are not split by language: the person writing the prompt has
    only one native language, and that file is for themselves, so making "which directory it goes in"
    depend on the UI language is both meaningless and would break when the language is switched.
    Built-in templates are the opposite——users may have different native languages, so they are split
    by language and fall back to the baseline.
    """
    langs = [current_lang()]
    if langs[0] != FALLBACK_LANG:
        langs.append(FALLBACK_LANG)

    paths: list[tuple[Path, str | None]] = []
    override = _override_root()
    if override is not None:
        paths.append((override / f"{feature}.md", None))
    paths.extend((_builtin_root() / lang / f"{feature}.md", lang) for lang in langs)
    return paths


def template(feature: str) -> PromptTemplate:
    """Get a template by priority (cached in-process)."""
    if feature in _cache:
        return _cache[feature]
    if not _FEATURE_RE.match(feature):
        raise PromptLoadError(f"invalid feature name: {feature!r}")

    errors: list[str] = []
    for path, lang in _candidate_paths(feature):
        if not path.is_file():
            continue
        try:
            tpl = _parse_file(path, feature)
        except (PromptLoadError, OSError, UnicodeDecodeError) as e:
            # A broken user override file must not stop execution (fall back to built-in), but it
            # must never be silent——otherwise "the prompt I changed didn't take effect" would be
            # impossible to discover. A broken built-in file is recorded and raised together at the end.
            #
            # The caught range must **also cover read failures**: catching only PromptLoadError
            # lets `read_text`'s OSError (deleted externally / insufficient permission) and
            # UnicodeDecodeError (saved back as non-UTF-8) pass straight through, making the line
            # above about "a user's mistake should not stall the feature" vacuous——the first stage
            # of the summarize chain has no try, so a single override file saved as GBK is enough to
            # break the whole pipeline. Swift's `PromptManager.loadTemplate` uses `try?` to move to
            # the next candidate, and both sides must behave identically.
            errors.append(str(e))
            logger.error(str(e))
            continue
        # Only built-in templates have "language fallback"; user overrides are language-independent
        # and must not be reported as a fallback
        if lang is not None and lang != current_lang():
            logger.warning(
                f"{feature}: language {current_lang()!r} has no such template, falling back to {FALLBACK_LANG!r} ({path})"
            )
        _cache[feature] = tpl
        return tpl

    raise PromptLoadError(
        f"prompt unavailable feature={feature!r}: {'; '.join(errors) or 'no candidate file found'}"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def prompt(feature: str, **variables) -> str:
    """Get and render a prompt (the sole entry point for Python-side prompts)."""
    return render(template(feature), variables)


def missing_json_keys(feature: str, parsed: dict | None) -> tuple[str, ...]:
    """Check which keys the model output is missing per the template's `json_keys` (not declared / not a dict → empty)."""
    if not isinstance(parsed, dict):
        return ()
    return tuple(k for k in template(feature).json_keys if k not in parsed)
