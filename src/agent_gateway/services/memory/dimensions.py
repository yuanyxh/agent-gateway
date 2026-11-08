"""Cross-project memory dimensions: enums, normalization, language detection.

Values are declared by the caller; nothing here infers a dimension from text
except `language_of`, which is the one field the write path may derive.
"""

MEMORY_TASK_TYPES = (
    "bug",
    "feature",
    "refactor",
    "maintenance",
    "review",
    "test",
    "deploy",
    "config",
    "research",
    "doc",
)
MEMORY_ROLES = (
    "background",
    "fact",
    "requirement",
    "confirmation",
    "feedback",
    "verification",
    "root_cause",
    "conclusion",
    "progress",
    "decision",
)
INTERACTION_TYPES = (
    "instruction",
    "confirmation",
    "correction",
    "prohibition",
    "preference",
)
LANGUAGE_TYPES = ("zh", "en", "mixed")

#: Structured fields copied onto a recalled row so the pipeline can filter or
#: boost on them. Names match the write-side metadata keys; `narrative_role`
#: avoids mem0's reserved `role`, which it overwrites with the message role.
GROUPING_DIMENSIONS = (
    "project",
    "subsystem",
    "task",
    "task_type",
    "ticket",
    "language",
    "interaction",
    "session_id",
    "narrative_role",
)

#: Longest accepted free-form dimension value; longer values are dropped rather
#: than truncated so a filter can never match a value the writer did not send.
DIMENSION_MAX_CHARS = 128


def enum_value(value, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    return text if text in allowed else ""


def dimension_text(value) -> str:
    """Normalize a free-form dimension: trim, collapse spaces, drop control chars."""
    text = " ".join(str(value or "").split())
    if not text or len(text) > DIMENSION_MAX_CHARS:
        return ""
    if any(ord(char) < 32 for char in text):
        return ""
    return text


def language_of(explicit, content) -> str:
    """Explicit language wins; an unknown explicit value blanks instead of guessing."""
    value = str(explicit or "").strip()
    if value in LANGUAGE_TYPES:
        return value
    if value:
        return ""
    text = str(content or "")
    if not text:
        return ""
    chinese = sum(1 for char in text if "一" <= char <= "鿿")
    letters = sum(1 for char in text if char.isascii() and char.isalpha())
    if chinese and chinese >= letters:
        return "zh"
    if letters and letters > chinese * 3:
        return "en"
    return "mixed" if chinese or letters else ""
