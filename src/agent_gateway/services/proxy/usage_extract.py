"""Request/response context and tool-output extraction."""

from __future__ import annotations

import hashlib
import json

from agent_gateway.services.proxy.proxy_normalize import _result_text as result_text
from agent_gateway.services.proxy.proxy_protocol import strip_injected_rules


from agent_gateway.services.proxy.usage_constants import CLIENT_TEXT, logger
from agent_gateway.services.proxy.usage_resolver import _TAG_RE


def _extract_user_text(content) -> str:
    """Extract plain text from message content, skipping registered client-injected tags.

    Compatible with the Anthropic (content list of blocks) and OpenAI (content string) formats.
    """
    if content is None:
        return ""
    if isinstance(content, list):
        text_parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        content = " ".join(text_parts) if text_parts else ""
    # Strip the injected rule block first, **then** clean tags: the rule block precedes
    # the real user input, and running tag cleanup first could let the angle brackets in
    # the rule body eat `---rule_end` as well, causing the strip function to fail to find
    # the end marker and discard the real user input after the block.
    content_str = strip_injected_rules(str(content))
    # Strip only registered tags (both open and close tags); keep the remaining text as-is
    return _TAG_RE.sub("", content_str).strip()


#: Continuation-prompt prefixes (each client has its own value) — their body is the
#: previous turn's summary, not user input, so both the session identifier and user-input
#: extraction must skip them. See CLIENT_TEXT for the source.
CONTINUATION_PREFIXES = CLIENT_TEXT.continuation_prefixes

#: Compression-summary prefixes — the continuation request body after context compression
#: begins with it, and it is likewise the previous turn's summary rather than user input,
#: so it is skipped along with the continuation prompt (both input_preview and
#: actual_user_input read these two constants).
SUMMARY_PREFIXES = CLIENT_TEXT.summary_prefixes

#: The threshold (chars) for a "large user message" that misses the registered text. A user
#: message exceeding this length that is neither a continuation prompt nor a summary prefix
#: most likely comes from **unregistered** client-injected content. Register a sample so the
#: new client's text can be added to config.yaml modelProxy.clientText — otherwise it only
#: silently pollutes actual_user_input and the session identifier anchor (both are inputs to
#: downstream statistics and routing decisions).
UNREGISTERED_BLOB_MIN_CHARS = 2000


_OBSERVED_BLOBS: set[str] = set()
_OBSERVED_BLOB_LOG_LIMIT = 20


def _observe_unregistered_blob(content_str: str) -> None:
    if len(content_str) < UNREGISTERED_BLOB_MIN_CHARS:
        return
    # The same unregistered text repeats every turn: log each distinct head once, cap the total.
    sample = content_str[:80]
    if sample in _OBSERVED_BLOBS or len(_OBSERVED_BLOBS) >= _OBSERVED_BLOB_LOG_LIMIT:
        return
    _OBSERVED_BLOBS.add(sample)
    logger.info(
        json.dumps(
            {
                "msg": "client_text_miss",
                "length": len(content_str),
                "sample": sample,
            }
        )
    )


#: Truncation cap for the session identifier's system prefix (same order of magnitude as
#: proxy_normalize.SYSTEM_PREFIX_LIMIT). The cost of recomputing SHA256 per request for a
#: long system grows with the session; truncation does not affect the discriminating power
#: of "grouping the same kind of agent".
SESSION_SYSTEM_LIMIT = 4096

#: Truncation cap for the session identifier's first user instruction. The first instruction
#: is the task anchor; later feedback/confirmation turns do not affect it.
SESSION_FIRST_USER_LIMIT = 2048


def compute_session_key(body: dict) -> str:
    """Request body → session identifier (first 12 hex chars of SHA256).

    Semantics: same system + tools + first user instruction = same session. Used to
    aggregate session-level total token consumption and measure "whether filtering really
    saves money" — saving tokens once does not equal a lower total task cost (truncation
    makes the model re-read files and increases turns, so the total cost may rise).

    This function is implemented independently and relies on no upper-layer policy —
    aggregating sessions is purely about recording, unrelated to the forwarding layer.
    """
    parts: list[str] = []

    # system: **both protocols must be read**, not just the top-level field.
    # Anthropic's system is a top-level field (string or an array of text blocks);
    # OpenAI has no such field, its system is a message with role="system" in
    # messages[0]. Reading only the top level makes the system of an OpenAI request
    # always empty — session keys of different agents (the main/sub agent's system
    # differs) degenerate into the same one, and aggregate analysis is directly distorted.
    system = body.get("system", "")
    if not system:
        for msg in body.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                system = msg.get("content", "")
                break
    if isinstance(system, list):
        system = "\n".join(
            b.get("text", "")
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if system:
        parts.append(str(system)[:SESSION_SYSTEM_LIMIT])

    # tools: sort by name to eliminate client sort differences
    tool_names = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict):
            tool_names.append(fn.get("name", ""))
        else:
            tool_names.append(t.get("name", ""))
    parts.append("\x01".join(sorted(n for n in tool_names if n)))

    # first_user: skip the continuation prompt (its body is the previous turn's summary, not a user instruction)
    for msg in body.get("messages", []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_user_text(msg.get("content", ""))
        if text.startswith(CONTINUATION_PREFIXES):
            continue
        if text:
            parts.append(text[:SESSION_FIRST_USER_LIMIT])
            break

    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def extract_request_context(body: dict) -> dict:
    """Extract context information from the Claude Code request body."""
    ctx = {
        "request_type": "conversation",
        "context_summary": "",
        "input_preview": "",
        "actual_user_input": "",
        "input_message_count": 0,
        "has_tool_result": 0,
    }

    messages = body.get("messages", [])
    ctx["input_message_count"] = len(messages)

    if body.get("tools"):
        ctx["request_type"] = "tool_use"

    # Check whether there is a tool_result (compatible with both formats)
    for msg in messages:
        if msg.get("role") == "tool":
            ctx["has_tool_result"] = 1
            ctx["request_type"] = "tool_result"
            break
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ctx["has_tool_result"] = 1
                    ctx["request_type"] = "tool_result"
                    break

    # Extract the last user message as context_summary
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content_str = _extract_user_text(msg.get("content", ""))
            ctx["context_summary"] = content_str[:100]
            break

    # input_preview: concatenate the first 200 chars of all user messages, skipping continuation prompts
    user_texts = []
    for msg in messages:
        if msg.get("role") == "user":
            content_str = _extract_user_text(msg.get("content", ""))
            # skip continuation prompts
            if content_str.startswith(CONTINUATION_PREFIXES):
                continue
            # skip pure summary content
            if content_str.strip().startswith(SUMMARY_PREFIXES):
                continue
            # a long message that misses the registered text → register a sample (discover unregistered client injections)
            _observe_unregistered_blob(content_str)
            if content_str.strip():
                user_texts.append(content_str[:200])
    ctx["input_preview"] = " | ".join(user_texts)

    # actual_user_input: extract the user's actual input, skipping continuation prompts and system-reminder
    actual_parts = []
    for msg in messages:
        if msg.get("role") == "user":
            content_str = _extract_user_text(msg.get("content", ""))
            # skip the continuation prompt (its body is the previous turn's summary, not user input)
            if content_str.startswith(CONTINUATION_PREFIXES):
                continue
            # skip pure summary content
            if content_str.strip().startswith(SUMMARY_PREFIXES):
                continue
            # a long message that misses the registered text → register a sample (discover unregistered client injections)
            _observe_unregistered_blob(content_str)
            if content_str.strip():
                actual_parts.append(content_str[:500])
    ctx["actual_user_input"] = " | ".join(actual_parts) if actual_parts else ""

    return ctx


def extract_response_context(usage_data: dict) -> dict:
    """Extract response context from the usage data."""
    stop_reason = (
        usage_data.get("stop_reason")
        or (usage_data.get("delta") or {}).get("stop_reason")
        or ""
    )
    # Only error and empty responses count as invalid; max_tokens/end_turn/stop_sequence are all normal
    response_valid = 0 if stop_reason == "error" else 1

    # Extract error information
    error_message = ""
    if stop_reason == "error":
        error_data = usage_data.get("error", {})
        if isinstance(error_data, dict):
            error_message = error_data.get("message", str(error_data))
        elif isinstance(error_data, str):
            error_message = error_data
    elif usage_data.get("error"):
        error_data = usage_data.get("error", {})
        if isinstance(error_data, dict):
            error_message = error_data.get("message", str(error_data))
        elif isinstance(error_data, str):
            error_message = error_data

    return {
        "output_type": "text",
        "stop_reason": stop_reason,
        "response_valid": response_valid,
        "error_message": error_message,
    }


def extract_tool_outputs(body: dict, api_format: str = "anthropic") -> list[dict]:
    """Extract all tool outputs of the latest round of tool_result from the request body.

    Only the most recent round of tool_result is extracted — historical tool_result were
    already recorded in earlier requests; each request carries the full message history but
    old tool_result are not recorded again.

    The output text is extracted with `proxy_normalize._result_text` (the authoritative
    implementation for tool return content) and is **not cleaned at all** — this is the data
    source for shape analysis, and stripping injected rules or removing client tags would
    distort content like "the file happens to contain `<system-reminder>`". Rule injection
    only affects the last real user message and never lands on tool_result blocks, so no
    stripping is needed here anyway.

    Returns a list; each item contains: tool_name, tool_call_id, output_text, output_size,
    output_lines, is_error.
    """
    messages = body.get("messages", [])
    if not messages:
        return []

    tool_results = []

    if api_format == "openai":
        # OpenAI: role="tool" is a standalone message; search backwards for consecutive tool messages
        for msg in reversed(messages):
            if msg.get("role") != "tool":
                break
            text = result_text(msg.get("content", ""))
            tool_results.append(
                {
                    "tool_name": _resolve_openai_tool_name(messages, msg),
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "output_text": text,
                    "output_text_raw": text,  # equals output_text when there is no filtering
                    "output_size": len(text.encode("utf-8")),
                    "output_lines": text.count("\n") + 1 if text else 0,
                    "is_error": None,  # OpenAI has no is_error field
                }
            )
        tool_results.reverse()  # restore chronological order
    else:
        # Anthropic: a role=user message contains content blocks with type=="tool_result"
        # search backwards for the last user message containing tool_result
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            blocks = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if not blocks:
                continue
            # look up the paired tool_use to get the tool name
            tool_names = _resolve_tool_names(messages, blocks)
            for b in blocks:
                text = result_text(b.get("content", ""))
                is_err = b.get("is_error")
                tool_results.append(
                    {
                        "tool_name": tool_names.get(b.get("tool_use_id", ""), ""),
                        "tool_call_id": b.get("tool_use_id", ""),
                        "output_text": text,
                        "output_text_raw": text,  # equals output_text when there is no filtering
                        "output_size": len(text.encode("utf-8")),
                        "output_lines": text.count("\n") + 1 if text else 0,
                        # three states: True=failure / False=confirmed success / None=not provided by the protocol
                        "is_error": (
                            1 if is_err is True else (0 if is_err is False else None)
                        ),
                    }
                )
            break  # only process the last user message containing tool_result

    return tool_results


def _resolve_openai_tool_name(messages: list[dict], tool_msg: dict) -> str:
    """OpenAI tool message → paired tool_call function name.

    OpenAI role="tool" messages carry no tool name and require looking up the tool_calls
    in the assistant message by tool_call_id.
    """
    call_id = tool_msg.get("tool_call_id", "")
    if not call_id:
        return ""
    for msg in reversed(messages):
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id") == call_id:
                fn = tc.get("function") or {}
                return fn.get("name", "") if isinstance(fn, dict) else ""
    return ""


def _resolve_tool_names(
    messages: list[dict], tool_result_blocks: list[dict]
) -> dict[str, str]:
    """Look up the tool_use tool names paired with tool_result from the message history.

    Returns a {tool_use_id: tool_name} mapping.
    """
    needed_ids = {
        b.get("tool_use_id", "") for b in tool_result_blocks if b.get("tool_use_id")
    }
    if not needed_ids:
        return {}

    names: dict[str, str] = {}
    for msg in reversed(messages):
        if len(names) == len(needed_ids):
            break
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            bid = block.get("id", "")
            if bid in needed_ids and bid not in names:
                names[bid] = block.get("name", "")
    return names
