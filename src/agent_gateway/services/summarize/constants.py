"""Constants and thresholds for the summarize pipeline."""

import os
import re
from pathlib import Path

from agent_gateway.core.config import (
    data_dir,
    notes_dir,
    note_exclude_dirs,
    note_extensions,
)
from agent_gateway.core.l10n import all_templates
from agent_gateway.core.log_utils import setup_logging as _log_setup
from agent_gateway.services.summarize.summary_llm import (
    SUMMARY_LLM_TIMEOUT,
)

logger = _log_setup("summarize")
NOTES_DIR = Path(notes_dir())

# Empty string = injected by the Swift layer via an environment variable; when not injected, related features are disabled
MEMORY_SERVER_URL = os.environ.get("MEMORY_SERVER_URL", "")

#: Directories skipped when reading the notes tree. Empty = skip nothing; the
#: list is fully user-configured (`mcp.noteExcludeDirs`). Put "Extracts" there
#: if summaries must not rescan their own output.
SUMMARY_EXCLUDE_DIRS = note_exclude_dirs()

#: Extensions listed in the notes-tree prompt. Empty config = markdown only.
SUMMARY_NOTE_EXTENSIONS = note_extensions((".md",))

SUMMARY_MEMORY_DAYS = 3

SUMMARY_MEMORY_LIMIT = 200

EXTRACTS_DIR = NOTES_DIR / "Extracts"

SUMMARY_MIN_CONTENT_LEN = 150

SUMMARY_GIT_TIMEOUT = 30

AGGREGATE_BATCH_THRESHOLD = 60

AGGREGATE_BATCH_SIZE = 60

AGGREGATE_LLM_TEMPERATURE = 0.2

#: LLM timeout for batch aggregation. Same source (config `ai.llmTimeout`) -- the
#: aggregate output is longer (max_tokens relaxed to 16384), but it hits the same
#: upstream timeout, so it shares the value with single summary instead of each
#: writing its own.
AGGREGATE_LLM_TIMEOUT = SUMMARY_LLM_TIMEOUT

AGGREGATE_MAX_ROUNDS = 3

SUMMARY_KEYWORD_MAX_TOKENS = 8192

SUMMARY_JUDGE_MAX_TOKENS = 8192

SUMMARY_GEN_MAX_TOKENS = 16384

SUMMARY_SEARCH_LIMIT = 5

SEARCH_MIN_TOKEN_LEN = 2

SUMMARY_KEYWORD_COUNT = 8

SUMMARY_MERGE_CONTEXT_CHARS = 2000

SUMMARY_MERGE_LOOKBACK_CHARS = 8000

SUMMARY_DUP_SIM_THRESHOLD = 0.7

SUMMARY_TOPIC_JACCARD = 0.05

SUMMARY_FRAGMENT_LINE_MIN = 10


def _marker_set(key: str) -> tuple[str, ...]:
    """Union of a newline-separated marker list across languages.

    Detection must work for content generated under either language (and for memories
    written before the language changed), so every language's markers are accepted.
    """
    markers: list[str] = []
    for template in all_templates(key).values():
        markers.extend(line.strip() for line in template.splitlines() if line.strip())
    return tuple(dict.fromkeys(markers))


SUMMARY_PLACEHOLDER_PATTERNS = _marker_set("mcp.summary.placeholder_markers")

MEMORY_QUALITY_MIN_LEN = 15

MEMORY_QUALITY_RESIDUAL_MIN = 12

MEMORY_QUALITY_HALF_MAX = 60

MEMORY_QUALITY_CN_SHORT_MAX = 50

MEMORY_QUALITY_REVIEW_VOTES = 3

MEMORY_QUALITY_LLM_THREADS = 4

QUALITY_TRUNC_PLACEHOLDERS = _marker_set("mcp.summary.truncation_markers")

_QUALITY_TRUNC_RE = re.compile(
    r"(?:内容|相关)?内容截断[（(]?[，,、]?(?:时|此处)?[）)]?仅?保留(?:已有|此)(?:信息|内容)"
    r"|(?:content\s+)?truncated[,;:]?\s*(?:only\s+)?(?:the\s+)?(?:existing\s+)?information\s+(?:kept|retained)"
    r"|only\s+(?:the\s+)?(?:existing|this)\s+information\s+(?:kept|retained)",
    re.IGNORECASE,
)

QUALITY_NONTRUNC_PLACEHOLDERS = _marker_set("mcp.summary.placeholder_markers")

QUALITY_PLACEHOLDER_SUBS = _marker_set("mcp.summary.placeholder_subterms")

_QUALITY_EMPTY_TITLE = re.compile(r"^[-*#]?\s*(?:\*\*)?[^：:]{2,40}(?:\*\*)?：\s*$")

_QUALITY_HALF_TRUNC = re.compile(r"(?:\.{3,}$|Stre$|undefi$|lody_ses$|：$|->\s*$)")

_QUALITY_HALF_CN = re.compile(r"(?:配置文|说明文|使用方|处理方|的具体|的详细)$")

_QUALITY_USER_SNAPSHOT = re.compile(r"^(User|User's)\b", re.IGNORECASE)

_QUALITY_CN_STATE_VERBS = (
    "完成",
    "测试",
    "验证",
    "回归",
    "配置",
    "通过",
    "运行",
    "部署",
    "发布",
    "检查",
)

_QUALITY_QUOTE_PAIRS = [("“", "”"), ("「", "」"), ("『", "』"), ("'", "'"), ('"', '"')]


UTILITY_DATA_DIR = Path(data_dir())

LOCK_FILE = UTILITY_DATA_DIR / "summarize.lock"

LAST_RUN_FILE = UTILITY_DATA_DIR / "summarize_last_run"

MIN_RUN_INTERVAL = 6 * 3600

COLD_SYNC_DB = UTILITY_DATA_DIR / "cold_sync.sqlite"
