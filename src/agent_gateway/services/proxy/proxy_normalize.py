"""Protocol text extraction utilities for token_proxy.

Reads the `system` field and message `content` as plain text under both the
Anthropic and OpenAI protocols. Rule injection (proxy_protocol.py) and tool
output extraction (proxy_usage.py) both reuse this module, avoiding deriving
"how to read these two fields" at each of the two call sites.
"""

from __future__ import annotations

API_ANTHROPIC = "anthropic"
API_OPENAI = "openai"


def system_to_text(system) -> str:
    """Anthropic system (string or array of text blocks) → plain text.

    This function is the **single authoritative implementation** of "how to read
    the system field" — the rule injection in `proxy_protocol.py` must reuse it,
    avoiding deriving the same fact twice.
    """
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "")
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _result_text(content) -> str:
    """tool_result / tool message content → plain text (str or array of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""
