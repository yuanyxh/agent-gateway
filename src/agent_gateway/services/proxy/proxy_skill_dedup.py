"""De-duplication of tool_result content in inbound requests.

When the same skill (MCP tool) is called repeatedly within one session, the
whole SKILL.md is re-inserted each time — measured: 6 byte-identical copies of
15184 B. This module replaces the second copy onward with a short note, keeping
the first (the model can still read the full content above).

The criterion is **byte-identical content**, not the tool name: the proxy cannot
see the skill list (that list lives inside the MCP server), and identical
content means identical semantics. A side effect is that repeated non-skill
output (two identical `ls`) is also collapsed — harmless and token-saving all
the same.

Placed at the proxy layer rather than the MCP server layer: the server has no
view of session history and can only track "already returned" via in-process
state; with multiple sessions sharing it, session A's load state leaks to B (B
receives "already loaded" while the content above is missing → silent failure,
among the hardest to troubleshoot).
"""

from __future__ import annotations

from agent_gateway.core.l10n import t

API_OPENAI = "openai"

#: Placeholder when the tool name cannot be mapped, avoiding rendering "skill  loaded above"
UNKNOWN_NAME = "?"


def note_for(name: str, lang: str) -> str:
    """Render the short note in the given language via the shared catalog."""
    return t("proxy.dedup.note", lang=lang, name=name or UNKNOWN_NAME)


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


def _resolve_anthropic_names(messages: list[dict]) -> dict[str, str]:
    """tool_use_id → tool name (the name is on the tool_use block of the assistant message)."""
    names: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[block.get("id", "")] = block.get("name", "")
    return names


def _resolve_openai_names(messages: list[dict]) -> dict[str, str]:
    """tool_call_id → tool name (the name is in the assistant message's tool_calls)."""
    names: dict[str, str] = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            names[call.get("id", "")] = (
                fn.get("name", "") if isinstance(fn, dict) else ""
            )
    return names


def _dedup_anthropic(messages: list[dict], lang: str) -> int:
    names = _resolve_anthropic_names(messages)
    seen: set[str] = set()
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
            if text in seen:
                name = names.get(block.get("tool_use_id", ""), "")
                _set_tool_result_text(block, note_for(name, lang))
                replaced += 1
            else:
                seen.add(text)
    return replaced


def _dedup_openai(messages: list[dict], lang: str) -> int:
    names = _resolve_openai_names(messages)
    seen: set[str] = set()
    replaced = 0
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if content in seen:
            name = names.get(msg.get("tool_call_id", ""), "")
            msg["content"] = note_for(name, lang)
            replaced += 1
        else:
            seen.add(content)
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
