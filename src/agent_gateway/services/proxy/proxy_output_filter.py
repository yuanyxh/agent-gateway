"""Shell tool output slimming for inbound requests.

Three layers, decreasing in safety:
  L1 head/tail trim — over max_bytes keep the first 70% + last 30% and insert a
     self-evident marker (on by default)
  L2 format compression — lossless JSON minify (on by default); repeated-line
     folding is off by default, since it changes the line count and is not lossless
  L3 command rules — drop_lines/keep_lines/max_bytes per shell command (empty table
     by default: measured, this project's Bash output ≤4KB accounts for 61% and
     everything >8KB is content-type, with none of snip's template noise)

**Only effective for shell-like tools**: Read/Edit etc. are file contents the model
explicitly asked for, and truncating them is corruption; shell output is the source
of "commands vary wildly and results are unpredictable". See `_shell_kind` for
detection.

Isomorphic with proxy_tool_output_dedup: both modify messages in place before the
proxy forwards, and both are called after record (the record layer keeps the
original text, otherwise the measurement yardstick destroys itself).
"""

from __future__ import annotations

import json
import re

from agent_gateway.core.l10n import t

API_OPENAI = "openai"

#: The config env name (injected by Swift `TokenProxyManager.outputFilterEnvKey`). This
#: env does not go through `config.py`, so contract group 3 cannot cover it; the two
#: sides' identical value is therefore asserted separately by the contract script.
OUTPUT_FILTER_ENV_KEY = "MODEL_PROXY_OUTPUT_FILTER"

#: Built-in shell tool names. The name differs across clients
#: (Gemini=`run_shell_command`, Cursor=`run_terminal_cmd`, Codex=`local_shell`), so this
#: is only a **fallback list**; the real criterion is the `command` in the input (a
#: protocol-level fact). Config can supplement it incrementally.
SHELL_TOOLS_DEFAULT = ("Bash",)

DEFAULT_MAX_BYTES = 8000
HEAD_RATIO = 0.7
FOLD_MIN_RUN = 3
FOLD_MIN_LEN = 4


def _mark(lang: str, n: int) -> str:
    return t("proxy.filter.truncated", lang=lang, n=n)


def _get_text(container: dict) -> str | None:
    """Get the container text: both the Anthropic tool_result block and the OpenAI tool message use content."""
    content = container.get("content")
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


def _set_text(container: dict, text: str) -> None:
    """Replace text in place, preserving the shell and pairing fields
    (tool_use_id/tool_call_id must not be touched).

    The only difference from the same-named function in proxy_skill_dedup is that this
    one does not check the block type, because OpenAI tool messages have no type field.
    """
    if isinstance(container.get("content"), str):
        container["content"] = text
    else:
        container["content"] = [{"type": "text", "text": text}]


def _anthropic_items(messages: list[dict]):
    """Yield (container, tool name, command). The command is taken from the assistant's tool_use.input."""
    names: dict[str, tuple[str, str | None]] = {}
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            cmd = inp.get("command") if isinstance(inp, dict) else None
            names[block.get("id", "")] = (block.get("name", ""), cmd)
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            name, cmd = names.get(block.get("tool_use_id", ""), ("", None))
            yield block, name, cmd


def _openai_items(messages: list[dict]):
    """Yield (container, tool name, command). The OpenAI function.arguments is a JSON string."""
    names: dict[str, tuple[str, str | None]] = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            fn = fn if isinstance(fn, dict) else {}
            cmd = None
            if isinstance(fn.get("arguments"), str):
                try:
                    parsed = json.loads(fn["arguments"])
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    cmd = parsed.get("command")
            names[call.get("id", "")] = (fn.get("name", ""), cmd)
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        name, cmd = names.get(msg.get("tool_call_id", ""), ("", None))
        yield msg, name, cmd


def _shell_kind(name: str, command, shell_tools: set[str]) -> str:
    """``listed`` = name is in the list; ``struct`` = not in the list but input carries command; ``""`` = not shell."""
    if name in shell_tools:
        return "listed"
    if isinstance(command, str) and command.strip():
        return "struct"
    return ""


def _truncate(text: str, max_bytes: int, lang: str) -> tuple[str, int]:
    """If over the limit, keep head and tail and insert a marker; returns (new text, bytes saved)."""
    data = text.encode()
    if max_bytes <= 0 or len(data) <= max_bytes:
        return text, 0
    head_n = int(max_bytes * HEAD_RATIO)
    tail_n = max_bytes - head_n
    head = data[:head_n].decode("utf-8", "ignore")
    tail = data[-tail_n:].decode("utf-8", "ignore") if tail_n > 0 else ""
    dropped = len(data) - len(head.encode()) - len(tail.encode())
    if dropped <= 0:
        return text, 0
    return head + _mark(lang, dropped) + tail, dropped


def _minify_json(text: str) -> tuple[str, int]:
    """Lossless JSON minify (whitespace only); returns as-is for non-JSON or parse failure."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return text, 0
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text, 0
    if not isinstance(parsed, (dict, list)):
        return text, 0
    minified = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(minified) >= len(text):
        return text, 0
    return minified, len(text.encode()) - len(minified.encode())


def _fold_repeated(text: str) -> tuple[str, int]:
    """Fold ≥3 consecutive identical lines into `line (xN)`. **Not lossless** (the line count changes), hence off by default."""
    lines = text.split("\n")
    out: list[str] = []
    saved = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        j = i + 1
        while j < len(lines) and lines[j] == line:
            j += 1
        if j - i >= FOLD_MIN_RUN and len(line.strip()) >= FOLD_MIN_LEN:
            out.append(f"{line} (x{j - i})")
            saved += sum(len(x.encode()) + 1 for x in lines[i + 1 : j])
        else:
            out.extend(lines[i:j])
        i = j
    if not saved:
        return text, 0
    return "\n".join(out), saved


def _match_rule(rules: list, command) -> dict | None:
    """Match a rule by the shell command's first token (synonymous with snip's match.command)."""
    if not isinstance(command, str) or not command.strip():
        return None
    parts = command.split()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("command") != parts[0]:
            continue
        sub = rule.get("subcommand")
        if sub and (len(parts) < 2 or parts[1] != sub):
            continue
        return rule
    return None


def _apply_rule(text: str, rule: dict) -> tuple[str, int]:
    """First drop/keep lines per the rule; returns (new text, bytes saved)."""
    drops = rule.get("drop_lines") or []
    keeps = rule.get("keep_lines") or []
    if not drops and not keeps:
        return text, 0
    out = []
    for line in text.split("\n"):
        if any(re.search(p, line) for p in drops):
            continue
        if keeps and not any(re.search(p, line) for p in keeps):
            continue
        out.append(line)
    new = "\n".join(out)
    return new, max(0, len(text.encode()) - len(new.encode()))


def _parse_config(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def filter_tool_outputs(
    body: dict,
    api_format: str,
    raw_config: str,
    lang: str,
) -> dict:
    """Slim the shell tools' tool_result text in place, returning stats; returns {} when unchanged."""
    cfg = _parse_config(raw_config)
    if not cfg.get("enabled", True):
        return {}
    messages = body.get("messages")
    if not isinstance(messages, list):
        return {}

    shell_tools = set(SHELL_TOOLS_DEFAULT) | set(cfg.get("shell_tools") or [])
    bypass = set(cfg.get("bypass_tools") or [])
    rules = cfg.get("rules") or []
    max_bytes = cfg.get("max_bytes") or DEFAULT_MAX_BYTES
    compress = cfg.get("format_compress", True) is not False
    fold = bool(cfg.get("fold_repeated_lines", False))

    items = (
        _openai_items(messages)
        if api_format == API_OPENAI
        else _anthropic_items(messages)
    )
    truncated = minified = folded = saved = 0
    unlisted: list[str] = []
    for container, name, command in items:
        text = _get_text(container)
        if text is None or not text.strip():
            continue
        kind = _shell_kind(name, command, shell_tools)
        if not kind or name in bypass:
            continue
        if kind == "struct" and name not in unlisted and len(unlisted) < 5:
            unlisted.append(name)
        original = text
        rule = _match_rule(rules, command)
        if rule:
            text, n = _apply_rule(text, rule)
            saved += n
        if compress:
            text, n = _minify_json(text)
            minified += 1 if n else 0
        if fold:
            text, n = _fold_repeated(text)
            folded += 1 if n else 0
        limit = int(rule.get("max_bytes") or max_bytes) if rule else max_bytes
        text, n = _truncate(text, limit, lang)
        if n:
            truncated += 1
        if text == original:
            continue
        _set_text(container, text)
        saved += len(original.encode()) - len(text.encode())

    if not saved and not unlisted:
        return {}
    return {
        "truncated": truncated,
        "minified": minified,
        "folded": folded,
        "saved_bytes": saved,
        "unlisted_tools": unlisted,
    }
