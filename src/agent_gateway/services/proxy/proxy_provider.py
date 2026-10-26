"""Field contract for model proxy provider configuration.

The providers list is serialized by Swift `TokenProxyManager.providersJSON()`
and passed in via the `MODEL_PROXY_PROVIDERS` environment variable;
`token_proxy.py` reads it to decide which upstream to forward to.

This module is the **single parsing entry point** for these fields: when the
field names are written in two places, a rename on the Swift side silently
makes both sides fall back to defaults — the protocol conversion direction
goes wrong and the auth key is lost, with nothing visible in the logs. Now
that it is centralized here, such drift needs only one place changed, and
validation and warnings can be added here.
"""

from __future__ import annotations

import json

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.proxy.proxy_normalize import API_ANTHROPIC, API_OPENAI

logger = setup_logging("token_proxy", json_format=True)

#: JSON-side key names, uniformly snake_case (mapped by Swift-side CodingKeys; the
#: YAML user-facing surface is still camelCase).
KEY_NAME = "name"
KEY_URL = "url"
KEY_API_FORMAT = "api_format"
KEY_API_KEY = "api_key"
KEY_MODELS = "models"

#: Legacy spellings (the Swift side used to be camelCase). Kept for reading only
#: to support hand-written env debugging; new code always reads via the snake_case
#: keys above.
_LEGACY_FORMAT_KEY = "apiFormat"
_LEGACY_API_KEY = "apiKey"

#: For a provider to take part in routing, these keys must at least be non-empty.
REQUIRED_KEYS = (KEY_NAME, KEY_URL, KEY_MODELS)


def api_format_of(provider: dict) -> str:
    """The provider's protocol format: `anthropic` or `openai` (unknown/missing falls back to anthropic).

    Falling back to anthropic is the conservative choice (most providers use
    anthropic-compatible endpoints), but it **must leave a trace**: an illegal
    value means the key names or enums on the Swift and Python sides have already
    drifted, and a silent fallback would send the protocol conversion in the wrong
    direction — the request goes out and works, just in the wrong format, the
    hardest class of failure to troubleshoot.
    """
    value = provider.get(KEY_API_FORMAT) or provider.get(_LEGACY_FORMAT_KEY)
    if value in (API_ANTHROPIC, API_OPENAI):
        return value
    if value:
        logger.warning(
            json.dumps(
                {
                    "msg": "provider_api_format_invalid",
                    "provider": provider.get(KEY_NAME, ""),
                    "value": str(value),
                    "effect": "fall back to anthropic",
                }
            )
        )
    return API_ANTHROPIC


def api_key_of(provider: dict) -> str:
    """The provider's auth key (empty string = pass through the client's own key when forwarding)."""
    return provider.get(KEY_API_KEY) or provider.get(_LEGACY_API_KEY) or ""


def missing_keys(provider: dict) -> list[str]:
    """Return the required keys missing from this provider (empty list = complete config)."""
    return [k for k in REQUIRED_KEYS if not provider.get(k)]
