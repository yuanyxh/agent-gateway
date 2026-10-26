"""Usage field path resolution."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from agent_gateway.llm import client as llm_client
from agent_gateway.core.db import conn as dbconn


from agent_gateway.services.proxy.usage_constants import CLIENT_TEXT, DB_PATH, logger


class UsageFieldResolver:
    """Intelligently locate the usage field path by provider/model.

    Known provider path mappings (avoiding LLM callbacks):
      - Anthropic/DeepSeek: usage.input_tokens / usage.output_tokens
      - OpenAI: usage.prompt_tokens / usage.completion_tokens
    """

    # Known field paths (model name prefix match → paths dict)
    _KNOWN_PATTERNS: list[tuple[str, dict]] = [
        (
            "anthropic",
            {
                "input_path": "usage.input_tokens",
                "output_path": "usage.output_tokens",
                "cache_read_path": "usage.cache_read_input_tokens",
                "cache_write_path": "usage.cache_creation_input_tokens",
            },
        ),
        (
            "claude",
            {
                "input_path": "usage.input_tokens",
                "output_path": "usage.output_tokens",
                "cache_read_path": "usage.cache_read_input_tokens",
                "cache_write_path": "usage.cache_creation_input_tokens",
            },
        ),
        (
            "deepseek",
            {
                "input_path": "usage.input_tokens",
                "output_path": "usage.output_tokens",
                "cache_read_path": "usage.cache_read_input_tokens",
                "cache_write_path": "usage.cache_creation_input_tokens",
            },
        ),
        (
            "gpt",
            {
                "input_path": "usage.prompt_tokens",
                "output_path": "usage.completion_tokens",
                "cache_read_path": "usage.prompt_tokens_details.cached_tokens",
                "cache_write_path": "",
            },
        ),
        (
            "o1",
            {
                "input_path": "usage.prompt_tokens",
                "output_path": "usage.completion_tokens",
                "cache_read_path": "",
                "cache_write_path": "",
            },
        ),
        (
            "o3",
            {
                "input_path": "usage.prompt_tokens",
                "output_path": "usage.completion_tokens",
                "cache_read_path": "",
                "cache_write_path": "",
            },
        ),
    ]

    def __init__(self):
        self._cache: dict[tuple[str, str], dict] = {}

    def _load_from_db(self, provider: str, model: str) -> dict | None:
        try:
            with dbconn(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT input_path, output_path, cache_read_path, cache_write_path "
                    "FROM usage_field_cache WHERE provider=? AND model=?",
                    (provider, model),
                ).fetchone()
            if row:
                return {
                    "input_path": row[0],
                    "output_path": row[1],
                    "cache_read_path": row[2],
                    "cache_write_path": row[3],
                }
        except Exception:
            pass
        return None

    def _save_to_db(self, provider: str, model: str, paths: dict):
        try:
            with dbconn(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO usage_field_cache "
                    "(provider, model, input_path, output_path, cache_read_path, cache_write_path, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        provider,
                        model,
                        paths.get("input_path", ""),
                        paths.get("output_path", ""),
                        paths.get("cache_read_path", ""),
                        paths.get("cache_write_path", ""),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except Exception:
            pass

    async def _ask_model(self, sample_response: dict) -> dict | None:
        """Call the main model to identify the usage field path."""
        if not llm_client.is_configured():
            return None

        prompt = (
            "Analyze this API response JSON and identify the exact field paths for token usage. "
            "Return ONLY a JSON object with these keys: "
            "input_path, output_path, cache_read_path, cache_write_path. "
            "Each value is a dot-separated path like 'usage.input_tokens'. "
            "If a field doesn't exist, use empty string.\n\n"
            "Note: streaming SSE responses may nest usage under 'delta', "
            "e.g. delta.usage.input_tokens. Use the ACTUAL structure in the sample.\n\n"
            f"Response sample:\n{json.dumps(sample_response, indent=2)[:2000]}"
        )

        raw = await llm_client.achat(
            prompt,
            temperature=0,
            max_tokens=300,
            timeout=15,
            label="usage_resolver_ask_failed",
        )
        return llm_client.parse_json(raw)

    def _get_by_path(self, obj: dict, path: str):
        """Get a value by dot-separated path."""
        if not path:
            return None
        parts = path.split(".")
        cur = obj
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    async def resolve(self, response_body: dict, provider: str = "default") -> dict:
        """Resolve the usage fields, returning {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}."""
        model = response_body.get("model", "unknown")
        key = (provider, model)

        paths = self._cache.get(key) or self._load_from_db(provider, model)

        if not paths:
            # Try matching against known patterns
            for prefix, known_paths in self._KNOWN_PATTERNS:
                if model.lower().startswith(prefix):
                    paths = known_paths
                    self._cache[key] = paths
                    self._save_to_db(provider, model, paths)
                    break

        if not paths:
            # Auto-detect from the response body's usage field names
            usage_data = response_body.get("usage", {})
            if isinstance(usage_data, dict):
                if "input_tokens" in usage_data:
                    paths = {
                        "input_path": "usage.input_tokens",
                        "output_path": "usage.output_tokens",
                        "cache_read_path": "usage.cache_read_input_tokens",
                        "cache_write_path": "usage.cache_creation_input_tokens",
                    }
                elif "prompt_tokens" in usage_data:
                    paths = {
                        "input_path": "usage.prompt_tokens",
                        "output_path": "usage.completion_tokens",
                        "cache_read_path": "usage.prompt_tokens_details.cached_tokens",
                        "cache_write_path": "",
                    }
            if paths:
                self._cache[key] = paths
                self._save_to_db(provider, model, paths)

        if not paths:
            # First time seeing this model; call the model to identify
            paths = await self._ask_model(response_body)
            if paths:
                self._cache[key] = paths
                self._save_to_db(provider, model, paths)
                logger.info(
                    json.dumps(
                        {
                            "msg": "usage_resolver_new_model",
                            "model": model,
                            "paths": paths,
                        }
                    )
                )
            else:
                # Cannot identify; return zero values
                return self._zero_usage()

        # Extract values using the cached paths
        usage = {
            "input_tokens": self._safe_int(
                self._get_by_path(response_body, paths.get("input_path", ""))
            ),
            "output_tokens": self._safe_int(
                self._get_by_path(response_body, paths.get("output_path", ""))
            ),
            "cache_read_tokens": self._safe_int(
                self._get_by_path(response_body, paths.get("cache_read_path", ""))
            ),
            "cache_write_tokens": self._safe_int(
                self._get_by_path(response_body, paths.get("cache_write_path", ""))
            ),
        }

        # If all extracted values are 0, the paths may be stale; refresh once
        if all(v == 0 for v in usage.values()):
            refreshed_paths = await self._ask_model(response_body)
            if refreshed_paths and refreshed_paths != paths:
                self._cache[key] = refreshed_paths
                self._save_to_db(provider, model, refreshed_paths)
                usage = {
                    "input_tokens": self._safe_int(
                        self._get_by_path(
                            response_body, refreshed_paths.get("input_path", "")
                        )
                    ),
                    "output_tokens": self._safe_int(
                        self._get_by_path(
                            response_body, refreshed_paths.get("output_path", "")
                        )
                    ),
                    "cache_read_tokens": self._safe_int(
                        self._get_by_path(
                            response_body, refreshed_paths.get("cache_read_path", "")
                        )
                    ),
                    "cache_write_tokens": self._safe_int(
                        self._get_by_path(
                            response_body, refreshed_paths.get("cache_write_path", "")
                        )
                    ),
                }
                logger.info(
                    json.dumps(
                        {
                            "msg": "usage_resolver_refreshed",
                            "model": model,
                            "new_paths": refreshed_paths,
                        }
                    )
                )

        return usage

    @staticmethod
    def _safe_int(val) -> int:
        if val is None:
            return 0
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _zero_usage() -> dict:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }


resolver = UsageFieldResolver()


#: **Registered** client-injected tags to strip from user text.
#:
#: Strip by name only; do not treat any `<...>` as a tag: when the user input contains
#: comparison operators (`if x < y and y > z`) or HTML fragments, a generic tag cleanup
#: eats the whole middle section — measured: `'if x < y and y > z: pass'` was cleaned to
#: `'if x z: pass'`, seriously distorting the log fields.
CLIENT_INJECTED_TAGS = CLIENT_TEXT.injected_tags

_TAG_RE = re.compile(
    r"</?(?:" + "|".join(re.escape(t) for t in CLIENT_INJECTED_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)
