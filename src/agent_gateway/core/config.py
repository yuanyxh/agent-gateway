"""Shared config reading layer.

The duplicated env reads and defaults of the three monoliths
(server.py / summarize.py / token_proxy.py) are consolidated here,
eliminating "the same variable read in 2-3 files, defaults hardcoded
and scattered" duplication.

Usage (flat same-directory import, reusing the log_utils/agent_call pattern):
    from agent_gateway.core.config import ai_api_key, summary_model, summary_api_url, notes_dir, data_dir

Notes:
- This module is a pure function layer depending only on the os standard
  library; importing it has no side effects -- server.py's module-level
  side-effect ordering (setting POSTHOG_DISABLED/MEM0_TELEMETRY before
  importing mem0) is unaffected.
- All returns are expanded strings (defaults run through expanduser); the
  caller decides whether to wrap them in Path (e.g. summarize.py wraps with
  Path(...)).
- Script-specific env (MCP_PORT / MODEL_PROXY_* / QDRANT_PATH / EMBEDDER_MODEL
  etc.) is not in this layer -- only common variables duplicated across files
  are consolidated.
- SUMMARY_MODEL / SUMMARY_API_URL defaults differ per script: server/summarize
  use the real defaults, token_proxy uses an empty string as a
  "unconfigured = disable LLM calls" guard (see token_proxy _ask_model), so the
  accessors accept a default argument and behave exactly as before the split.
"""

from __future__ import annotations

import json
import os

from agent_gateway.core import config_loader, paths

# Empty string = injected by the Swift layer via env var; when not injected, the related feature is disabled
_DEFAULT_SUMMARY_MODEL = ""
_DEFAULT_SUMMARY_API_URL = ""
_DEFAULT_NOTES_DIR = ""
_DEFAULT_DATA_DIR = ""

#: env name -> (config.yaml dotted path, serialization). Only read when Swift did not inject the env.
_YAML_FALLBACKS: dict[str, tuple[str, str]] = {
    "AI_API_KEY": ("ai.apiKey", "text"),
    "AI_LLM_TIMEOUT": ("ai.llmTimeout", "text"),
    "UTILITY_LANG": ("general.language", "text"),
    "SUMMARY_MODEL": ("ai.model", "text"),
    "SUMMARY_API_URL": ("ai.baseURL", "text"),
    "NOTES_DIR": ("mcp.knowledgeBasePath", "text"),
    "SUMMARY_EXCLUDE_TAGS": ("mcp.excludeTags", "csv"),
    "UTILITY_LOG_MAX_SIZE": ("general.logMaxSize", "text"),
    "UTILITY_LOG_BACKUP_COUNT": ("general.logBackupCount", "text"),
    "NOTE_EXCLUDE_DIRS": ("mcp.noteExcludeDirs", "json"),
    "NOTE_EXTENSIONS": ("mcp.noteExtensions", "json"),
    "KNOWLEDGE_TOPICS": ("knowledge.topics", "json"),
    "KNOWLEDGE_TOPIC_TARGET": ("knowledge.poolTargetTopic", "text"),
    "KNOWLEDGE_MAX_FAIL_STREAK": ("knowledge.maxFailStreak", "text"),
    "KNOWLEDGE_RETENTION_DAYS": ("knowledge.retentionDays", "text"),
}


def _serialize(node: object, kind: str) -> str:
    """Render a YAML value in the exact shape its env consumer expects."""
    if kind == "json":
        return json.dumps(node, ensure_ascii=False)
    if kind == "csv":
        if isinstance(node, list):
            return ",".join(str(item) for item in node)
        return str(node)
    return str(node)


def get(key: str, default: str = "") -> str:
    """Unified config read: env first, config.yaml fallback, then `default`."""
    raw = os.environ.get(key)
    if raw is not None:
        return raw
    spec = _YAML_FALLBACKS.get(key)
    if spec is None:
        return default
    node = config_loader.value(spec[0])
    if node is None or node == "" or node == [] or node == {}:
        return default
    return _serialize(node, spec[1])


def ai_api_key() -> str:
    """Generic AI API key (shared by text generation and vector embedding, decoupled from provider).

    The env var `AI_API_KEY` is injected by Swift `EnvKeys.aiAPIKey`; the
    consistency of both sides is asserted by
    `scripts/check-router-config-contract.py`.
    """
    return get("AI_API_KEY", "")


def llm_timeout(default: float = 120.0) -> float:
    """LLM single-inference timeout (seconds). The env var `AI_LLM_TIMEOUT` is injected by Swift from `ai.llmTimeout`.

    Missing / non-numeric / non-positive values all fall back to `default`,
    consistent with the "0 = use default" convention of Swift's
    `OpenAIClient.init`. **Do not** apply the `general.httpTimeout` convention
    of "0 = unlimited": the same 0 means the opposite in the two configs and is
    the easiest way to misconfigure "never timeout".
    """
    raw = get("AI_LLM_TIMEOUT", "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def ui_language(default: str = "") -> str:
    """UI/prompt language (`zh-Hans` / `en`). Swift injects `UTILITY_LANG` from `general.language`.

    The prompt directory is selected by it (`prompts/<lang>/<feature>.md`), so
    it must share the same source as the UI language. When not injected it
    returns an empty string, and the caller (prompt_loader) falls back to the
    baseline language.
    """
    return get("UTILITY_LANG", default)


def summary_model(default: str = _DEFAULT_SUMMARY_MODEL) -> str:
    return get("SUMMARY_MODEL", default)


def summary_api_url(default: str = _DEFAULT_SUMMARY_API_URL) -> str:
    return get("SUMMARY_API_URL", default)


def notes_dir(default: str = _DEFAULT_NOTES_DIR) -> str:
    raw = get("NOTES_DIR", default)
    if not raw:
        # Empty `mcp.knowledgeBasePath` derives from the config root, same as the Swift host.
        base = data_dir()
        raw = os.path.join(base, "notes") if base else ""
    if not raw:
        return ""
    return os.path.expanduser(raw)


def data_dir(default: str = _DEFAULT_DATA_DIR) -> str:
    """State root: `GATEWAY_HOME` first, legacy config value second, then the default home."""
    raw = os.environ.get("GATEWAY_HOME", "").strip() or get("UTILITY_DATA_DIR", default)
    if not raw:
        return str(paths.gateway_home())
    return os.path.abspath(os.path.expanduser(raw))


def summary_exclude_tags() -> set[str]:
    """Read the excluded tag list (comma-separated) from env var SUMMARY_EXCLUDE_TAGS."""
    raw = get("SUMMARY_EXCLUDE_TAGS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def memory_protect_importance() -> int | None:
    """Importance at or above which contradiction checks skip a memory."""
    raw = get("MEMORY_PROTECT_IMPORTANCE", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def memory_max_content_chars() -> int | None:
    """Soft write limit for one fact; empty means no limit."""
    raw = get("MEMORY_MAX_CONTENT_CHARS", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def rank_signal_weights() -> dict[str, float]:
    """Optional ranking signals; all zero means plain score ordering."""
    out = {}
    for name, env in (
        ("recency", "RANK_SIGNAL_RECENCY"),
        ("access", "RANK_SIGNAL_ACCESS"),
        ("quality", "RANK_SIGNAL_QUALITY"),
    ):
        try:
            out[name] = float(get(env, "0") or 0)
        except ValueError:
            out[name] = 0.0
    return out


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def log_max_size(default: int = 10 * 1024 * 1024) -> int:
    """Log file size limit before rotation (`general.logMaxSize`, injected by Swift)."""
    value = _int(get("UTILITY_LOG_MAX_SIZE", ""), default)
    return value if value > 0 else default


def log_backup_count(default: int = 3) -> int:
    """Number of rotated log backups kept (`general.logBackupCount`, injected by Swift)."""
    value = _int(get("UTILITY_LOG_BACKUP_COUNT", ""), default)
    return value if value > 0 else default


def _json_list(raw: str) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _json_strings(raw: str) -> list[str]:
    return [v for v in _json_list(raw) if isinstance(v, str)]


def note_exclude_dirs() -> set[str]:
    """Note directory names to skip (JSON array). Empty = exclude nothing.

    Entries match by basename at any depth, so "Private" and "/notes/Private"
    are equivalent. ".", ".." and glob-looking entries are ignored.
    """
    names: set[str] = set()
    for value in _json_strings(get("NOTE_EXCLUDE_DIRS", "")):
        name = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name and name not in {".", ".."} and not any(c in name for c in "*?[]"):
            names.add(name)
    return names


def note_extensions(default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Note file extensions (JSON array). Empty/invalid = ``default``.

    "md" / ".MD" / "*.md" all normalize to ".md"; entries containing path
    separators or leftover glob characters are ignored.
    """
    out: list[str] = []
    for value in _json_strings(get("NOTE_EXTENSIONS", "")):
        ext = value.strip().lower()
        if ext.startswith("*."):
            ext = ext[1:]
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext == "." or any(c in ext for c in "*?[]/\\"):
            continue
        if ext not in out:
            out.append(ext)
    return tuple(out) or tuple(default)


def knowledge_topics() -> list[dict]:
    """主题种子（JSON 数组，元素 {name, kind, scope, exclude}）。"""
    return [t for t in _json_list(get("KNOWLEDGE_TOPICS", "")) if isinstance(t, dict)]


def knowledge_topic_target(default: int = 5) -> int:
    return _int(get("KNOWLEDGE_TOPIC_TARGET", ""), default)


def knowledge_max_fail_streak(default: int = 3) -> int:
    return _int(get("KNOWLEDGE_MAX_FAIL_STREAK", ""), default)


def knowledge_retention_days(default: int = 365) -> int:
    return _int(get("KNOWLEDGE_RETENTION_DAYS", ""), default)
