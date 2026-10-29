"""Protocol conversion and request injection layer for token_proxy.

Split out from token_proxy.py (2026-08-30, refactor stage 2): anthropic↔openai
protocol conversion (ProtocolConverter/OpenAIToAnthropicStream), safety classifier
detection and injection, system rule injection (_inject_system_prompt), SSE stream
extraction. The code was moved verbatim without logic changes.
"""

from __future__ import annotations

import json
import os
import time

from agent_gateway.core.config import notes_dir
from agent_gateway.core.log_utils import setup_logging

# The single authoritative implementation of "how to read the Anthropic system
# field" lives in the normalization layer and is reused here, avoiding deriving the
# same fact twice (when the system structure is extended, only one place changes).
# Dependency direction: proxy_protocol → proxy_normalize (the latter depends only on
# the standard library, so no cycle).
from agent_gateway.services.proxy.proxy_normalize import (
    system_to_text as _system_to_text,
)

# ---------------------------------------------------------------------------
# Configuration (identical to token_proxy.py before the split)
# ---------------------------------------------------------------------------

NOTES_DIR = notes_dir()
SYSTEM_PROMPTS_FILE = os.path.join(NOTES_DIR, "MCP", "System_Prompts.md")

logger = setup_logging("token_proxy", json_format=True)


#: The start/end markers of the rule-injection block. Defined by the injector (this
#: module); the stripper (usage extraction in proxy_usage) reuses the same constants —
#: avoiding deriving "which marker the injector uses" and "which marker the stripper
#: recognizes" in two places.
RULE_START_MARK = "--rule_start"
RULE_END_MARK = "--rule_end"
SHORT_RULE_START_MARK = "--short_rule_start"
SHORT_RULE_END_MARK = "--short_rule_end"

#: Legacy markers written before 2026-10-29; still stripped so old blocks in
#: client-side history do not leak into user-text extraction.
LEGACY_RULE_START_MARK = "---rule_start"
LEGACY_RULE_END_MARK = "---rule_end"

#: Attention reminder injected after the first user message. Static English on
#: purpose: it is a code constant, never a rendered template, so its bytes cannot
#: drift between turns and break the cached prefix. It only points at the marker —
#: restating rule content here would fork the single source (`System_Prompts.md`).
#: Kept deliberately imperative: a soft reminder was ignored often enough to lose
#: adherence, so the wording names the consequence of ignoring it.
SHORT_RULE_TEXT = (
    "Self-check: follow the --rule_start / --rule_end rules. "
    "**Never ignore them; restart the step on violation.**"
)


def _rule_injection_config() -> tuple[str, int]:
    """Read `MODEL_PROXY_RULE_INJECTION` (JSON `{mode, fullEvery}`); defaults to progressive."""
    raw = os.environ.get("MODEL_PROXY_RULE_INJECTION", "")
    if not raw:
        return "progressive", 0
    try:
        cfg = json.loads(raw)
    except ValueError:
        return "progressive", 0
    if not isinstance(cfg, dict):
        return "progressive", 0
    mode = "full" if cfg.get("mode") == "full" else "progressive"
    try:
        every = int(cfg.get("fullEvery", 0) or 0)
    except (TypeError, ValueError):
        every = 0
    return mode, max(0, every)


RULE_INJECTION_MODE, RULE_FULL_EVERY = _rule_injection_config()


def _strip_blocks(text: str, start: str, end: str) -> str:
    """Remove every `start`…`end` block; an unclosed block discards the remainder."""
    if not text or start not in text:
        return text
    out: list[str] = []
    rest = text
    while True:
        i = rest.find(start)
        if i < 0:
            out.append(rest)
            break
        out.append(rest[:i])
        j = rest.find(end, i + len(start))
        if j < 0:
            break
        rest = rest[j + len(end) :]
    return "".join(out)


def strip_injected_rules(text: str) -> str:
    """Strip injected rule blocks, keeping the rest of the user text.

    The injected blocks are much longer than the 100~500 character extraction caps
    of usage records — without stripping, `context_summary` / `input_preview` /
    `actual_user_input` would all show rules and push the real input out. Legacy
    triple-hyphen blocks are stripped first: the current double-hyphen marker is a
    substring of them, so matching it first would leave a stray leading hyphen.
    """
    text = _strip_blocks(text, SHORT_RULE_START_MARK, SHORT_RULE_END_MARK)
    text = _strip_blocks(text, LEGACY_RULE_START_MARK, LEGACY_RULE_END_MARK)
    return _strip_blocks(text, RULE_START_MARK, RULE_END_MARK).strip()


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

#: OpenAI `finish_reason` → Anthropic `stop_reason` mapping. **Single authoritative
#: definition**, shared by the streaming conversion / non-streaming conversion / SSE
#: usage extraction.
#:
#: Maintaining it in separate places makes the same upstream response yield different
#: conclusions on different paths: content_filter once had a mapping only in the
#: record layer's table while both conversion layers lacked it, so
#: `mapping.get(finish, "end_turn")` treated "content filtered" as "normal end" — the
#: client saw end_turn and the routing layer also concluded normal termination from
#: it, the anomaly signal was lost entirely, while the database faithfully recorded
#: content_filter.
FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}

#: Fallback when the conversion layer meets an unknown finish_reason — must be a legal
#: Anthropic enum value, otherwise the client rejects the entire response for an
#: illegal stop_reason.
FALLBACK_STOP_REASON = "end_turn"

#: Anthropic `stop_reason` → OpenAI `finish_reason` (the reverse direction). Lossy by
#: protocol design: `end_turn` and `stop_sequence` both become `stop`.
STOP_REASON_MAP_REVERSE: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "error": "stop",
}

FALLBACK_FINISH_REASON = "stop"


def _tool_choice_anthropic_to_openai(choice):
    """Anthropic tool_choice → OpenAI tool_choice.

    Anthropic: {"type": "auto"|"any"|"tool"|"none"[, "name": ...]}
    OpenAI:    "auto"|"required"|"none" | {"type":"function","function":{"name":...}}
    """
    if not isinstance(choice, dict):
        return choice
    ctype = choice.get("type")
    if ctype == "any":
        return "required"
    if ctype == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return ctype or "auto"


def _tool_choice_openai_to_anthropic(choice):
    """OpenAI tool_choice → Anthropic tool_choice (the inverse of the previous function)."""
    if isinstance(choice, str):
        return {"type": {"required": "any", "none": "none"}.get(choice, "auto")}
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        return {"type": "tool", "name": fn.get("name", "")}
    return choice


class ProtocolConverter:
    """Bidirectional conversion of Anthropic Messages API ↔ OpenAI Chat Completions request bodies."""

    @staticmethod
    def request_anthropic_to_openai(body: dict) -> dict:
        out: dict = {
            "model": body.get("model", ""),
            "stream": body.get("stream", False),
        }
        if body.get("max_tokens"):
            out["max_tokens"] = body["max_tokens"]
        if body.get("temperature") is not None:
            out["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            out["top_p"] = body["top_p"]
        # Anthropic's stop-sequence field is `stop_sequences` (an array); the OpenAI
        # equivalent is `stop`. This used to read `stop` — Anthropic request bodies
        # have no such key, so the conversion never took effect (silently dropping config).
        if body.get("stop_sequences") is not None:
            out["stop"] = body["stop_sequences"]
        if body.get("tool_choice") is not None:
            out["tool_choice"] = _tool_choice_anthropic_to_openai(body["tool_choice"])

        system_text = _system_to_text(body.get("system"))
        messages: list[dict] = []
        if system_text.strip():
            messages.append({"role": "system", "content": system_text})

        for m in body.get("messages", []):
            role, content = m.get("role"), m.get("content")
            if role == "user":
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    text_parts: list[str] = []
                    tool_results: list[dict] = []
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text":
                            text_parts.append(b.get("text", ""))
                        elif bt == "tool_result":
                            tool_results.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": b.get("tool_use_id", ""),
                                    "content": b.get("content", ""),
                                }
                            )
                        elif bt == "image":
                            src = b.get("source", {})
                            if src.get("type") == "base64":
                                text_parts.append(
                                    "![image](data:{};base64,{})".format(
                                        src.get("media_type", ""), src.get("data", "")
                                    )
                                )
                    if text_parts:
                        messages.append(
                            {"role": "user", "content": "".join(text_parts)}
                        )
                    messages.extend(tool_results)
            elif role == "assistant":
                if isinstance(content, str):
                    messages.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    text_parts, tool_calls = [], []
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text":
                            text_parts.append(b.get("text", ""))
                        elif bt == "tool_use":
                            tool_calls.append(
                                {
                                    "id": b.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": b.get("name", ""),
                                        "arguments": json.dumps(
                                            b.get("input", {}), ensure_ascii=False
                                        ),
                                    },
                                }
                            )
                    msg = {"role": "assistant"}
                    if text_parts:
                        msg["content"] = "".join(text_parts)
                    if tool_calls:
                        msg["tool_calls"] = tool_calls
                    messages.append(msg)

        out["messages"] = messages

        tools = body.get("tools")
        if tools:
            out["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get(
                            "input_schema", {"type": "object", "properties": {}}
                        ),
                    },
                }
                for t in tools
                if isinstance(t, dict)
            ]
        if body.get("stream"):
            # openai streaming does not return usage by default; explicitly request it be carried at the end
            out["stream_options"] = {"include_usage": True}
        return out

    @staticmethod
    def request_openai_to_anthropic(body: dict) -> dict:
        out: dict = {
            "model": body.get("model", ""),
            "stream": body.get("stream", False),
        }
        if body.get("max_tokens"):
            out["max_tokens"] = body["max_tokens"]
        if body.get("temperature") is not None:
            out["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            out["top_p"] = body["top_p"]
        # OpenAI `stop` ↔ Anthropic `stop_sequences`: the field names differ on the two sides and must be converted explicitly
        if body.get("stop") is not None:
            out["stop_sequences"] = body["stop"]

        system_text = ""
        messages: list[dict] = []
        for m in body.get("messages", []):
            role = m.get("role")
            if role == "system":
                system_text += (m.get("content") or "") + "\n"
            elif role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.get("tool_call_id", ""),
                                "content": m.get("content", ""),
                            }
                        ],
                    }
                )
            elif role == "user":
                content = m.get("content")
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    blocks = []
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            blocks.append({"type": "text", "text": b.get("text", "")})
                    messages.append({"role": "user", "content": blocks})
            elif role == "assistant":
                content = m.get("content")
                blocks = []
                if content:
                    text = (
                        content
                        if isinstance(content, str)
                        else json.dumps(content, ensure_ascii=False)
                    )
                    blocks.append({"type": "text", "text": text})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        inp = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": inp,
                        }
                    )
                messages.append({"role": "assistant", "content": blocks})

        if system_text.strip():
            out["system"] = [{"type": "text", "text": system_text.strip()}]
        out["messages"] = messages

        tools = body.get("tools")
        if tools:
            out["tools"] = []
            for t in tools:
                if isinstance(t, dict) and t.get("type") == "function":
                    fn = t.get("function", {})
                    out["tools"].append(
                        {
                            "name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                            "input_schema": fn.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
        if body.get("tool_choice") is not None:
            out["tool_choice"] = _tool_choice_openai_to_anthropic(body["tool_choice"])
        return out

    @staticmethod
    def response_openai_to_anthropic(resp: dict) -> dict:
        """OpenAI Chat Completions non-streaming response → Anthropic Messages response."""
        choice = (resp.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        content_blocks = []
        text = message.get("content") or ""
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                inp = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": inp,
                }
            )
        usage = resp.get("usage", {})
        return {
            "id": resp.get("id", "msg_proxy"),
            "type": "message",
            "role": "assistant",
            "model": resp.get("model", ""),
            "content": content_blocks,
            "stop_reason": FINISH_REASON_MAP.get(
                choice.get("finish_reason"), FALLBACK_STOP_REASON
            ),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    @staticmethod
    def response_anthropic_to_openai(resp: dict) -> dict:
        """Anthropic Messages non-streaming response → OpenAI Chat Completions response."""
        content = resp.get("content") or []
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        tool_calls = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(
                                block.get("input") or {}, ensure_ascii=False
                            ),
                        },
                    }
                )
        message: dict = {"role": "assistant", "content": "".join(texts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = resp.get("usage") or {}
        prompt = usage.get("input_tokens", 0)
        completion = usage.get("output_tokens", 0)
        return {
            "id": resp.get("id", "chatcmpl_proxy"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": STOP_REASON_MAP_REVERSE.get(
                        resp.get("stop_reason"), FALLBACK_FINISH_REASON
                    ),
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }


class OpenAIToAnthropicStream:
    """openai SSE streaming response → anthropic SSE events (incremental conversion).

    State machine: after the first content block has accumulated enough, emit
    content_block_start, then deltas one by one; tool calls are mapped by the openai
    index to independent content blocks; at the end emit message_delta (stop_reason +
    usage) and message_stop. Block order assigns indices by "first appearance".
    """

    def __init__(self):
        self.blocks: list[int] = []
        self.text_block: int | None = None
        self.tool_blocks: dict[int, int] = {}
        self.tool_state: dict[int, dict] = {}
        self.sent_start = False
        self.stop_reason: str | None = None
        self.usage: dict = {}
        self.model = ""

    def feed(self, chunk: dict) -> list[str]:
        events: list[str] = []
        if chunk.get("model"):
            self.model = chunk["model"]

        if not self.sent_start:
            events.append(
                _sse_event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_proxy",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": self.model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                )
            )
            self.sent_start = True

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason")

            text = delta.get("content")
            if text:
                if self.text_block is None:
                    self.text_block = len(self.blocks)
                    self.blocks.append(self.text_block)
                    events.append(
                        _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": self.text_block,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    )
                events.append(
                    _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.text_block,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )

            for tc in delta.get("tool_calls") or []:
                t_idx = tc.get("index", 0)
                fn = tc.get("function", {})
                st = self.tool_state.get(t_idx, {"id": "", "name": ""})
                if tc.get("id"):
                    st["id"] = tc["id"]
                if fn.get("name"):
                    st["name"] = fn["name"]
                self.tool_state[t_idx] = st

                if t_idx not in self.tool_blocks:
                    block_idx = len(self.blocks)
                    self.blocks.append(block_idx)
                    self.tool_blocks[t_idx] = block_idx
                    events.append(
                        _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": block_idx,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": st["id"],
                                    "name": st["name"],
                                    "input": {},
                                },
                            },
                        )
                    )
                args = fn.get("arguments") or ""
                if args:
                    events.append(
                        _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": self.tool_blocks[t_idx],
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": args,
                                },
                            },
                        )
                    )

            if finish:
                self.stop_reason = FINISH_REASON_MAP.get(finish, FALLBACK_STOP_REASON)

        if chunk.get("usage"):
            u = chunk["usage"]
            self.usage = {
                "input_tokens": u.get("prompt_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0),
            }

        return events

    def finish(self) -> list[str]:
        events: list[str] = []
        for block_idx in self.blocks:
            events.append(
                _sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": block_idx},
                )
            )
        # input_tokens must be sent here: OpenAI's prompt_tokens only appears in the
        # last chunk of the stream, whereas message_start was sent long ago (which could
        # only write 0). Sending only output_tokens would make the client (Claude Code's
        # statusLine) always show input as 0.
        events.append(
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": self.stop_reason or FALLBACK_STOP_REASON,
                        "stop_sequence": None,
                    },
                    "usage": {
                        "input_tokens": self.usage.get("input_tokens", 0),
                        "output_tokens": self.usage.get("output_tokens", 0),
                    },
                },
            )
        )
        events.append(_sse_event("message_stop", {"type": "message_stop"}))
        return events


class AnthropicToOpenAIStream:
    """anthropic SSE streaming response → openai SSE chunks (incremental conversion).

    Text deltas map straight to `choices[0].delta.content`; tool_use blocks map to
    `tool_calls` entries (the anthropic block index is rewritten to a dense tool index);
    thinking deltas have no OpenAI equivalent and are dropped. `message_delta` carries
    the finish reason and output tokens, so the terminal chunk plus a usage-only chunk
    are emitted from `finish()`, followed by `[DONE]`.
    """

    def __init__(self) -> None:
        self.created = int(time.time())
        self.model = ""
        self.sent_role = False
        self.finish_reason: str | None = None
        self.usage: dict[str, int] = {}
        self.tool_index: dict[int, int] = {}
        self.next_tool = 0

    def _chunk(self, delta: dict, finish_reason: str | None) -> str:
        payload = {
            "id": "chatcmpl_proxy",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def feed(self, event: dict) -> list[str]:
        out: list[str] = []
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self.model = message.get("model", "")
            usage = message.get("usage") or {}
            if "input_tokens" in usage:
                self.usage["input_tokens"] = usage.get("input_tokens", 0)
            out.append(self._chunk({"role": "assistant"}, None))
            self.sent_role = True
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                block_index = event.get("index", 0)
                tool_index = self.next_tool
                self.next_tool += 1
                self.tool_index[block_index] = tool_index
                out.append(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        None,
                    )
                )
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                out.append(self._chunk({"content": delta.get("text", "")}, None))
            elif dtype == "input_json_delta":
                tool_index = self.tool_index.get(event.get("index", 0))
                if tool_index is not None:
                    out.append(
                        self._chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": tool_index,
                                        "function": {
                                            "arguments": delta.get("partial_json", "")
                                        },
                                    }
                                ]
                            },
                            None,
                        )
                    )
        elif etype == "message_delta":
            stop_reason = (event.get("delta") or {}).get("stop_reason")
            self.finish_reason = STOP_REASON_MAP_REVERSE.get(
                stop_reason, FALLBACK_FINISH_REASON
            )
            usage = event.get("usage") or {}
            if "output_tokens" in usage:
                self.usage["output_tokens"] = usage.get("output_tokens", 0)
        return out

    def finish(self) -> list[str]:
        out: list[str] = []
        if not self.sent_role:
            out.append(self._chunk({"role": "assistant"}, None))
        out.append(self._chunk({}, self.finish_reason or FALLBACK_FINISH_REASON))
        if self.usage:
            prompt = self.usage.get("input_tokens", 0)
            completion = self.usage.get("output_tokens", 0)
            out.append(
                "data: "
                + json.dumps(
                    {
                        "id": "chatcmpl_proxy",
                        "object": "chat.completion.chunk",
                        "created": self.created,
                        "model": self.model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "total_tokens": prompt + completion,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        out.append("data: [DONE]\n\n")
        return out


def _detect_api_format(body: dict) -> str:
    """Detect the API format from the request body: 'anthropic' or 'openai'."""
    if "system" in body and isinstance(body.get("system"), list):
        return "anthropic"
    messages = body.get("messages", [])
    if messages and messages[0].get("role") == "system":
        return "openai"
    tools = body.get("tools", [])
    if tools and isinstance(tools[0], dict):
        # Anthropic's tool-definition field is `input_schema` (snake_case). This used to
        # be mistakenly written as `inputSchema` (that is the spelling of MCP tools/list,
        # not any API format), making this branch never match and degenerate into dead
        # code, relying only on the trailing anthropic fallback to be "coincidentally correct".
        if "input_schema" in tools[0]:
            return "anthropic"
        if tools[0].get("type") == "function":
            return "openai"
    return "anthropic"


def _is_classifier_candidate(body: dict) -> bool:
    """Structural heuristic for whether this is a classifier candidate: stream != true + empty tools + messages ≤ 2.

    Does not match any hard-coded signature (the signature changes with the Claude Code
    version; generalization avoids silent failure). The v2.1.160+ classifier request
    carries an assistant pre-fill, with messages = 2; a real conversation has dozens to
    hundreds.
    """
    if body.get("stream"):
        return False
    if body.get("tools"):
        return False
    return len(body.get("messages", [])) <= 2


def _inject_classifier_disabled(body: dict, api_format: str) -> None:
    """Inject thinking disabled (deepseek compatibility layer) into the request body and remove the old-version parameters."""
    if api_format == "anthropic":
        body["thinking"] = {"type": "disabled"}
        for k in ("reasoning_effort", "output_config"):
            body.pop(k, None)
    else:
        # openai format: no thinking parameter, removing reasoning_effort suffices
        body.pop("reasoning_effort", None)


_sys_prompt_content: str | None = None

_sys_prompt_mtime: float = 0


def _is_real_user_input(content: object) -> bool:
    """True when the message carries user text rather than only tool results."""
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "text" for block in content
        )
    return False


def _starts_with_rule_marker(text: str) -> bool:
    """True when the text itself begins with one of our injection markers.

    Prefix-only on purpose: a body that merely mentions `--rule_start` (quoting
    this feature, or pasting the legacy triple-hyphen block) is still user text
    and must receive rules. Legacy blocks do not match — the third character is
    `-`, not `r` — so they get one harmless re-injection.
    """
    return text.lstrip().startswith((RULE_START_MARK, SHORT_RULE_START_MARK))


def _contains_rule_block(content: object) -> bool:
    """True when a message starts with an injected rule block."""
    if isinstance(content, str):
        return _starts_with_rule_marker(content)
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and _starts_with_rule_marker(block["text"])
            for block in content
        )
    return False


def _insert_rule_block(msg: dict, block: str) -> None:
    """Insert the block before user text, never before a leading tool_result."""
    content = msg.get("content", "")
    if isinstance(content, str):
        msg["content"] = block + content
        return
    last_tool_result = -1
    for idx, item in enumerate(content):
        if isinstance(item, dict) and item.get("type") == "tool_result":
            last_tool_result = idx
    content.insert(last_tool_result + 1, {"type": "text", "text": block})


def _inject_system_prompt(body: dict) -> tuple[bytes | None, int, int]:
    """Inject full rules into the first real user input, the short reminder into the rest.

    Position-only, so every historical message keeps identical bytes across requests
    and the prompt cache prefix stays stable; compaction simply yields a new first
    message. Returns (body_bytes, full, short).
    """
    global _sys_prompt_content, _sys_prompt_mtime

    try:
        current_mtime = os.path.getmtime(SYSTEM_PROMPTS_FILE)
    except OSError:
        _sys_prompt_content = None
        return None, 0, 0

    if current_mtime != _sys_prompt_mtime:
        try:
            with open(SYSTEM_PROMPTS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            _sys_prompt_content = content if content else None
            _sys_prompt_mtime = current_mtime
            if _sys_prompt_content and RULE_END_MARK in _sys_prompt_content:
                logger.warning(json.dumps({"msg": "system_prompt_contains_end_mark"}))
        except Exception as e:
            _sys_prompt_content = None
            logger.warning(
                json.dumps({"msg": "system_prompt_read_failed", "error": str(e)})
            )
            return None, 0, 0

    if _sys_prompt_content is None:
        return None, 0, 0

    full_block = f"{RULE_START_MARK}\n\n{_sys_prompt_content}\n{RULE_END_MARK}\n\n"
    short_block = (
        f"{SHORT_RULE_START_MARK}\n{SHORT_RULE_TEXT}\n{SHORT_RULE_END_MARK}\n\n"
    )
    messages = body.get("messages", [])
    full = short = 0
    real_index = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not _is_real_user_input(content):
            continue
        real_index += 1
        if _contains_rule_block(content):
            continue
        if RULE_INJECTION_MODE == "full":
            use_full = True
        else:
            use_full = real_index == 1 or (
                RULE_FULL_EVERY > 0 and (real_index - 1) % RULE_FULL_EVERY == 0
            )
        _insert_rule_block(msg, full_block if use_full else short_block)
        if use_full:
            full += 1
        else:
            short += 1

    if full + short == 0:
        logger.warning(
            json.dumps(
                {
                    "msg": "rule_inject_no_target",
                    "user_messages": sum(
                        1 for m in messages if m.get("role") == "user"
                    ),
                }
            )
        )

    return json.dumps(body).encode(), full, short


def _extract_usage_from_sse(
    sse_data: bytes, api_format: str = "anthropic"
) -> dict | None:
    """Extract the last chunk containing usage from the SSE stream.

    When api_format="openai":
      - the usage fields use prompt_tokens/completion_tokens
      - finish_reason is mapped to stop_reason
      - normalized to the input_tokens/output_tokens/cache_read_input_tokens field names
    """
    last_usage = None
    detected_model = ""
    detected_stop_reason = ""

    for line in sse_data.decode("utf-8", errors="replace").split("\n"):
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)

            if "model" in chunk:
                detected_model = chunk["model"]

            if api_format == "anthropic":
                sr = chunk.get("stop_reason") or (chunk.get("delta") or {}).get(
                    "stop_reason"
                )
                if sr:
                    detected_stop_reason = sr
            else:
                # OpenAI: finish_reason is in choices[0]
                choices = chunk.get("choices", [])
                if choices:
                    fr = choices[0].get("finish_reason")
                    if fr:
                        # Unified mapping table (see FINISH_REASON_MAP). Unknown values
                        # are kept as-is: the record layer must faithfully persist, and
                        # must not disguise an unknown reason as end_turn.
                        detected_stop_reason = FINISH_REASON_MAP.get(fr, fr)

            if "usage" in chunk:
                last_usage = chunk
        except json.JSONDecodeError:
            continue

    if last_usage:
        if detected_model:
            last_usage["model"] = detected_model
        if detected_stop_reason:
            last_usage["stop_reason"] = detected_stop_reason

        if api_format == "openai":
            # Normalize the OpenAI usage field names to the unified format
            u = last_usage.get("usage", {})
            if "prompt_tokens" in u:
                u["input_tokens"] = u["prompt_tokens"]
            if "completion_tokens" in u:
                u["output_tokens"] = u["completion_tokens"]
            details = u.get("prompt_tokens_details", {})
            if isinstance(details, dict) and "cached_tokens" in details:
                u["cache_read_input_tokens"] = details["cached_tokens"]

    return last_usage


def _extract_output_from_sse(sse_data: bytes, api_format: str = "anthropic") -> str:
    """Extract the output text content from the SSE stream (first 2000 chars)."""
    output_text = []
    for line in sse_data.decode("utf-8", errors="replace").split("\n"):
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)

            if api_format == "anthropic":
                # extract text from content_delta
                delta = chunk.get("delta", {})
                if "text" in delta:
                    output_text.append(delta["text"])
                # extract text from the content array
                content = chunk.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            output_text.append(block.get("text", ""))
            else:
                # OpenAI: choices[0].delta.content
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content is not None:
                        output_text.append(content)
        except json.JSONDecodeError:
            continue
    # take the first 2000 chars
    full_text = "".join(output_text)
    return full_text[:2000] if full_text else ""


def _extract_tool_name_from_sse(sse_data: bytes, api_format: str = "anthropic") -> str:
    """Extract the tool name of tool_use/tool_calls from the SSE stream."""
    if api_format == "anthropic":
        # Anthropic: content_block_start → content_block.name
        for line in sse_data.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                if chunk.get("type") == "content_block_start":
                    block = chunk.get("content_block", {})
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name:
                            return name
            except json.JSONDecodeError:
                continue
    else:
        # OpenAI: choices[0].delta.tool_calls[0].function.name
        for line in sse_data.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    tool_calls = delta.get("tool_calls", [])
                    if tool_calls:
                        for tc in tool_calls:
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            if name:
                                return name
            except json.JSONDecodeError:
                continue
    return ""
