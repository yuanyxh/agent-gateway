"""Shared LLM/network infrastructure for summarize.

Split out of summarize.py (2026-08-30, refactor phase 3): LLM calls, JSON parsing,
and error-log writing. Shared by the core pipeline (summarize.py) and phase 3 rules
extraction (summary_rules.py). Code was moved as-is, logic unchanged.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from agent_gateway.llm import client as llm_client
from agent_gateway.core.config import ai_api_key, notes_dir
from agent_gateway.core.log_utils import (
    setup_logging,
    write_error_log as _shared_write_error_log,
)

# ---------------------------------------------------------------------------
# Configuration (same as summarize.py before the split)
# ---------------------------------------------------------------------------

#: Primary model key. This module no longer uses it directly to send requests
#: (LLM calls all go through `llm_client`); it is kept because `summarize.py`'s
#: `main()` guard reads it from here (exits immediately when the key is missing).
AI_API_KEY = ai_api_key()
NOTES_DIR = Path(notes_dir())
ERROR_LOG = NOTES_DIR / "流水线错误日志.jsonl"
SUMMARY_LLM_TEMPERATURE = 0.1
#: LLM call timeout (seconds). Single source = `llm_client.DEFAULT_SYNC_TIMEOUT` <- config `ai.llmTimeout`.
#: Previously this was hardcoded to 90 here while `summary_rules`/`summarize` each
#: hardcoded 120 -- three values for the same kind of call.
SUMMARY_LLM_TIMEOUT = llm_client.DEFAULT_SYNC_TIMEOUT
SUMMARY_API_TIMEOUT = 10

logger = setup_logging("summarize")

_HTTP = httpx.Client(timeout=SUMMARY_API_TIMEOUT)


def write_error_log(msg: str, code: str = "UNKNOWN"):
    try:
        _shared_write_error_log(
            str(ERROR_LOG),
            {
                "code": code,
                "message": msg,
            },
        )
    except Exception as e:
        logger.error(f"Error log write failed: {e}")


def _llm_chat(
    prompt: str,
    temperature: float | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    disable_thinking: bool = False,
    system_prompt: str | None = None,
) -> str | None:
    """Thin synchronous LLM wrapper for the whole summarize chain (forwards `llm_client.chat`).

    Keeping this name instead of having the 6 call sites call `llm_client.chat`
    directly: the only differences between call sites are a few defaults
    (temperature / timeout / disable-thinking), which are fine to converge in one
    place rather than touching the call sites.
    """
    return llm_client.chat(
        prompt,
        system=system_prompt,
        temperature=(
            temperature if temperature is not None else SUMMARY_LLM_TEMPERATURE
        ),
        max_tokens=max_tokens,
        timeout=timeout if timeout is not None else SUMMARY_LLM_TIMEOUT,
        disable_thinking=disable_thinking,
        # Reasoning models: on long prompts the thinking
        # eats up all of max_tokens, content ends up empty -> JSON parse failure
        # (AGGREGATE_JSON root cause), so disable thinking for aggregate/generate.
        # This is a **private field** of that provider (not an OpenAI protocol fact)
        # and can only be passed explicitly by the caller: putting it into
        # llm_client's common layer would make other openai providers receive an
        # unknown field and return 400.
        extra_body={"thinking": {"type": "disabled"}} if disable_thinking else None,
        label="LLM call failed",
    )


#: Kept under its historical name: 6 call sites in `summarize.py` / `summary_rules.py`
#: depend on it. The implementation has moved to `llm_client.parse_json` (the former
#: `_escape_inline_quotes` moved along with it).
_parse_json_any = llm_client.parse_json
