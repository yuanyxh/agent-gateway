"""De-duplication of repeated tool_result content in inbound requests.

When identical output is returned repeatedly within one session, this module
replaces the second copy onward with a short note, keeping the first (the model
can still read the full content above).

The criterion is **byte-identical content**, not the tool name. A skill reload
and two identical `ls` calls are both collapsed; the proxy cannot see which tool
produced the text.

Placed at the proxy layer rather than the MCP server layer: the server has no
view of session history and can only track "already returned" via in-process
state; with multiple sessions sharing it, session A's load state leaks to B (B
receives "already loaded" while the content above is missing → silent failure,
among the hardest to troubleshoot).
"""

from __future__ import annotations

from agent_gateway.core.l10n import t

API_OPENAI = "openai"

#: Placeholder when the first occurrence carries no id, avoiding a dangling "tool result"
UNKNOWN_REF = "?"


def note_for(ref: str, lang: str) -> str:
    """Render the short note, pointing at the first occurrence's tool result id."""
    return t("proxy.dedup.note", lang=lang, id=ref or UNKNOWN_REF)


def _tool_result_text(block: dict) -> str | None:
    """Anthropic tool_result block → text; returns None for non-tool_result."""
    if block.get("type") != "tool_result":
        return None
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts) if parts else None
    return None


def _set_tool_result_text(block: dict, text: str) -> None:
    """Replace the text in place, **preserving the block's other fields and the
    message shell** — the pairing info (tool_use_id) must not be touched, or the
    upstream directly returns 400 for "tool_use missing its paired tool_result"."""
    content = block.get("content")
    if isinstance(content, str):
        block["content"] = text
    else:
        block["content"] = [{"type": "text", "text": text}]


def _dedup_anthropic(messages: list[dict], lang: str) -> int:
    first_id: dict[str, str] = {}
    replaced = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = _tool_result_text(block)
            if text is None or not text.strip():
                continue
            if text in first_id:
                _set_tool_result_text(block, note_for(first_id[text], lang))
                replaced += 1
            else:
                first_id[text] = block.get("tool_use_id", "")
    return replaced


def _dedup_openai(messages: list[dict], lang: str) -> int:
    first_id: dict[str, str] = {}
    replaced = 0
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if content in first_id:
            msg["content"] = note_for(first_id[content], lang)
            replaced += 1
        else:
            first_id[content] = msg.get("tool_call_id", "")
    return replaced


def dedup_tool_results(body: dict, api_format: str, lang: str) -> int:
    """De-duplicate repeated tool_result text in place, returning the number of
    entries replaced.

    Only the **first** occurrence of each text is kept — the model can still read
    the full content above.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    if api_format == API_OPENAI:
        return _dedup_openai(messages, lang)
    return _dedup_anthropic(messages, lang)
