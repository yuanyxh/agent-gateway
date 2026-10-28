"""Single-shot text summarization on top of the shared LLM client."""

from __future__ import annotations

from agent_gateway.llm import client as llm_client

MAX_INPUT_CHARS = 50_000
DEFAULT_INSTRUCTION = "Summarize the text in its own language."
OUTPUT_RULES = (
    "Return only the summary itself: no preamble, no headings, no commentary, "
    "no follow-up questions."
)
DEFAULT_MAX_TOKENS = 600


def summarize_text(
    text: str, instruction: str = "", max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict:
    """Summarize `text`; returns `{ok, summary, chars, input_truncated}` or an error."""
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "text is required"}
    truncated = len(body) > MAX_INPUT_CHARS
    style = (instruction or "").strip() or DEFAULT_INSTRUCTION
    summary = llm_client.chat(
        f"Summarize the following text:\n\n{body[:MAX_INPUT_CHARS]}",
        system=f"{style}\n{OUTPUT_RULES}",
        max_tokens=max(64, min(int(max_tokens), 4000)),
        timeout=120,
        label="summarize",
    )
    if summary is None:
        return {"ok": False, "error": "llm_unavailable"}
    result = summary.strip()
    return {
        "ok": True,
        "summary": result,
        "chars": len(result),
        "input_truncated": truncated,
    }
