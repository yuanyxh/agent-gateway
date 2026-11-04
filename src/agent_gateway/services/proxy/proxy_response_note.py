"""Append a line at the end of the response: "what the proxy rewrote this turn".

The model name comes from message_start.message.model in the stream; the list of
rewrites is generated from the output_filter / skill_dedup stats on the request
side.

**Only appended to the final reply** (the last content block is text) — a tool
call turn ends with a tool_use block, and the reader of that line is the user;
inserting it into a tool call is both meaningless and may interfere with the
client's parsing of the tool block. The criterion is the block type, not
stop_reason: the latter only appears at message_delta, by which time the content
block has already stopped and there is no time to add another delta.

The SSE injection point is a **deferred content_block_stop**: on receiving stop
it is buffered first, and only when the next event arrives is it known whether
it is the last — if so, a content_block_delta is inserted before it (reusing that
block's index), otherwise it is passed through as-is. No new block is created and
index increments need not be managed.

feed/finish both take and return bytes and internally use **incremental UTF-8
decoding**: SSE fragmentation can split a multi-byte character in half, and
per-fragment `decode()` would produce replacement characters and corrupt the
JSON.
"""

from __future__ import annotations

import codecs
import json
import os
import re

from agent_gateway.core.l10n import all_templates, compact_number, t

#: The line is built by joining these segments.
SEG_JOIN = " | "

#: Request-side stat name -> catalog key (the stats are internal identifiers; the text is not).
_PART_KEYS: dict[str, str] = {
    "outputFilter": "proxy.note.part.output_filter",
    "toolOutputDedup": "proxy.note.part.tool_output_dedup",
}

#: Above this many context tokens the line suggests starting a new session; 0 disables the hint.
#: Config surface `modelProxy.contextWarnTokens` -> this env (injected by Swift
#: `TokenProxyManager.contextWarnEnvKey`); like the response-note switch it does not go through
#: `config.py`, so the contract script asserts the two sides' names separately. Read once at
#: import: the env is fixed when the process starts, and re-reading per response would only add a
#: syscall per turn.
CONTEXT_WARN_ENV_KEY = "MODEL_PROXY_CONTEXT_WARN"
DEFAULT_CONTEXT_WARN_TOKENS = 300_000


def _load_warn_tokens() -> int:
    raw = os.environ.get(CONTEXT_WARN_ENV_KEY, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WARN_TOKENS


CONTEXT_WARN_TOKENS = _load_warn_tokens()

#: The gap between the appended line and the body
NOTE_SEPARATOR = "\n\n"

#: The switch env name (injected by Swift `TokenProxyManager.responseNoteEnvKey`). This
#: env does not go through `config.py`, so contract group 3 cannot cover it; the two
#: sides' identical value is therefore asserted separately by the contract script.
RESPONSE_NOTE_ENV_KEY = "MODEL_PROXY_RESPONSE_NOTE"


def _label(key: str, lang: str) -> str:
    catalog_key = _PART_KEYS.get(key)
    return t(catalog_key, lang=lang) if catalog_key else key


def context_tokens_of(usage: dict) -> int | None:
    """Total context this request carried, from the upstream's own usage numbers.

    `input_tokens` is only the uncached remainder — a long session reports a small input
    and a large cache_read, so summing all three is what actually approximates the context
    the model saw. Returns None when nothing is usable (no guess is better than a wrong one).
    """
    if not isinstance(usage, dict):
        return None
    total = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )
    return total or None


def _context_segment(context_tokens: int | None, lang: str) -> str | None:
    if not context_tokens or context_tokens <= 0:
        return None
    seg = t("proxy.note.context", lang=lang, ctx=compact_number(context_tokens, lang))
    if CONTEXT_WARN_TOKENS > 0 and context_tokens >= CONTEXT_WARN_TOKENS:
        seg += t("proxy.note.context_hint", lang=lang)
    return seg


def build_note(
    model: str, parts: list[str], lang: str, context_tokens: int | None = None
) -> str | None:
    """Build the appended line; **returns None only when the model name is
    unavailable** — the model name is the main information of this line and must be
    shown even when nothing was rewritten (this is the case in the first few turns
    of a new session)."""
    if not model:
        return None
    segs = [model]
    ctx = _context_segment(context_tokens, lang)
    if ctx:
        segs.append(ctx)
    if not parts:
        segs.append(t("proxy.note.empty", lang=lang))
    else:
        labels = "/".join(_label(p, lang) for p in parts)
        segs.append(t("proxy.note.applied", lang=lang, parts=labels))
    return SEG_JOIN.join(segs)


#: The note's trailing segment, derived from every language's template so the strip side
#: cannot drift from the build side (even if the UI language changed mid-session).
NOTE_APPLIED_PREFIXES: tuple[str, ...] = tuple(
    tpl.split("{parts}")[0] for tpl in all_templates("proxy.note.applied").values()
)
NOTE_EMPTY_EXACT: frozenset[str] = frozenset(all_templates("proxy.note.empty").values())

_MODEL_SEG_RE = re.compile(r"[\w.\-]+")


def is_note_line(line: str) -> bool:
    """Whether a line has the shape of the appended note — used to strip the model's echoes of it."""
    segs = [s.strip() for s in line.split(SEG_JOIN)]
    if len(segs) < 2 or not _MODEL_SEG_RE.fullmatch(segs[0]):
        return False
    tail = segs[-1]
    return tail in NOTE_EMPTY_EXACT or tail.startswith(NOTE_APPLIED_PREFIXES)


def strip_notes_from_text(text: str) -> tuple[str, int]:
    """Drop note-shaped lines along with the blank line that preceded each. Returns (text, count)."""
    out: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if is_note_line(line):
            removed += 1
            while out and not out[-1].strip():
                out.pop()
            continue
        out.append(line)
    return "\n".join(out), removed


def strip_notes_from_messages(messages: list) -> int:
    """Strip note echoes from **assistant** messages (both API formats). Returns the count removed.

    Only assistant messages are touched: the note is only ever produced on the response side, so
    user text (where a Markdown table row contains " | ") is left alone.
    """
    removed = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new, count = strip_notes_from_text(content)
            if count:
                msg["content"] = new
                removed += count
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                new, count = strip_notes_from_text(block.get("text") or "")
                if count:
                    block["text"] = new
                    removed += count
    return removed


def _data_of(event: str) -> dict:
    """Get the data JSON of an SSE event; returns {} when missing or unparseable."""
    for line in event.split("\n"):
        if line.startswith("data: "):
            try:
                parsed = json.loads(line[6:])
            except (json.JSONDecodeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def append_note_to_body(
    resp: dict,
    model: str,
    parts: list[str],
    lang: str,
    context_tokens: int | None = None,
) -> bool:
    """Non-streaming: merge the appended line into the last text block. Returns whether it changed anything."""
    note = build_note(model, parts, lang, context_tokens)
    if note is None:
        return False
    content = resp.get("content")
    if not isinstance(content, list) or not content:
        return False
    last = content[-1]
    if not isinstance(last, dict) or last.get("type") != "text":
        return False
    last["text"] = (last.get("text") or "") + NOTE_SEPARATOR + note
    return True


class SSENoteInjector:
    """Feed anthropic SSE bytes segment by segment, inserting the appended line before the final text block closes."""

    def __init__(self, parts: list[str], lang: str):
        self._parts = parts
        self._lang = lang
        self._note: str | None = None
        self._buf = ""
        self._block_type: dict = {}
        self._pending_stop: tuple | None = None
        # Hard stop at one note per response. The line answers "what did the proxy rewrite
        # this turn", so a second copy carries no information — and when the stream happens
        # to carry extra stop/delta cycles, an unbounded injector stacks the same line dozens
        # of times into the reply (observed: 86 identical lines in one message).
        self._injected = False
        #: How many `message_start` events this stream carried. A well-formed response has
        #: exactly one; more means another round's data leaked into the same stream (observed:
        #: two notes with different context sizes in one reply). Counted only to make that
        #: leak observable — the injection cap above already keeps it from showing twice.
        self.message_starts = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes) -> bytes:
        """Return the bytes that should be written to the client — may be shorter than the input (stop was buffered)."""
        self._buf += self._decoder.decode(chunk)
        out: list[str] = []
        while True:
            idx = self._buf.find("\n\n")
            if idx < 0:
                break
            event, self._buf = self._buf[: idx + 2], self._buf[idx + 2 :]
            out.append(self._handle(event))
        return "".join(out).encode()

    def finish(self) -> bytes:
        """End of stream: any residual stop must be the last block, so appending is safe."""
        if not self._buf and self._pending_stop is None:
            return b""
        out = self._release_pending(is_last=True) + self._buf
        self._buf = ""
        return out.encode()

    def _handle(self, event: str) -> str:
        data = _data_of(event)
        etype = data.get("type")
        index = data.get("index")
        if etype == "content_block_start":
            block = data.get("content_block")
            self._block_type[index] = (block or {}).get("type")
            return self._release_pending(is_last=False) + event
        if etype == "content_block_stop":
            self._pending_stop = (index, event)
            return ""
        if etype == "message_start":
            self.message_starts += 1
            message = data.get("message") or {}
            model = message.get("model") or ""
            self._note = build_note(
                model,
                self._parts,
                self._lang,
                context_tokens_of(message.get("usage") or {}),
            )
        elif etype in ("message_delta", "message_stop"):
            return self._release_pending(is_last=True) + event
        return event

    def _release_pending(self, is_last: bool) -> str:
        if self._pending_stop is None:
            return ""
        index, raw = self._pending_stop
        self._pending_stop = None
        if not is_last or not self._note:
            return raw
        return self._note_delta(index) + raw

    def _note_delta(self, index) -> str:
        """Only append when a text block closes; tool_use blocks are left as-is."""
        if self._injected:
            return ""
        if self._block_type.get(index) != "text":
            return ""
        self._injected = True
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": NOTE_SEPARATOR + self._note},
        }
        body = json.dumps(payload, ensure_ascii=False)
        return f"event: content_block_delta\ndata: {body}\n\n"


class NoteBodyBuffer:
    """Non-streaming response: buffer the full body first, then parse and append."""

    def __init__(self, parts: list[str], lang: str):
        self._parts = parts
        self._lang = lang
        self._raw = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        self._raw.extend(chunk)
        return b""

    def finish(self) -> bytes:
        raw = bytes(self._raw)
        try:
            resp = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
        if not isinstance(resp, dict):
            return raw
        model = resp.get("model") or ""
        ctx = context_tokens_of(resp.get("usage") or {})
        if not append_note_to_body(resp, model, self._parts, self._lang, ctx):
            return raw
        return json.dumps(resp, ensure_ascii=False).encode()
