"""Proxy configuration, provider routing, and shared HTTP session."""

import json
import os

import aiohttp
from aiohttp import web
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.proxy.proxy_client import (
    load_client_accel,
)
from agent_gateway.services.proxy.proxy_output_filter import (
    OUTPUT_FILTER_ENV_KEY,
)
from agent_gateway.services.proxy.proxy_provider import (
    missing_keys,
)
from agent_gateway.services.proxy.proxy_response_note import (
    RESPONSE_NOTE_ENV_KEY,
)

# ---------------------------------------------------------------------------
# Configuration (dedicated env; common config in config.py, protocol/usage config in their own submodules)
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("MODEL_PROXY_PORT", "8080"))
UPSTREAM_URL = os.environ.get(
    "MODEL_PROXY_URL", ""
)  # compatibility: single upstream when providers is empty
# providers routing table (JSON env, serialized on the Swift side from config.yaml):
#   [{"name","url","apiFormat":"anthropic|openai","apiKey","models":[".."]}, ...]
# Routing rule: body.model exactly matches some provider.models → that provider (upstream
# model uses the original value); no match → providers[0] as fallback, upstream model uses
# its models[0].
PROVIDERS_JSON = os.environ.get("MODEL_PROXY_PROVIDERS", "")
# classifier acceleration's client allowlist (JSON env, serialized on the Swift side from
# config.yaml modelProxy.clientAccel; empty string by default → proxy_client uses the
# built-in default allowlist, i.e. historical behavior)
CLIENT_ACCEL_JSON = os.environ.get("MODEL_PROXY_CLIENT_ACCEL", "")
# skill full-text dedup switch (config.yaml modelProxy.skillDedupEnabled → env). On by
# default: when the same skill is called repeatedly, the whole SKILL.md is re-injected each
# time, pure waste.
SKILL_DEDUP_ENABLED = os.environ.get("MODEL_PROXY_SKILL_DEDUP", "1") != "0"
# Tool-output slimming config (config.yaml modelProxy.outputFilter → env, JSON string).
# Empty string by default means the Python-side built-in default (on, max_bytes 8000),
# isomorphic with skill dedup.
OUTPUT_FILTER_JSON = os.environ.get(OUTPUT_FILTER_ENV_KEY, "")
# Response-side append of "what was rewritten this turn" (config.yaml modelProxy.responseAnnotation → env).
RESPONSE_NOTE_ENABLED = os.environ.get(RESPONSE_NOTE_ENV_KEY, "1") != "0"

#: The response compression encoding whitelist **declared by ourselves** when forwarding to
#: the upstream (not passing through the client's Accept-Encoding).
#:
#: The upstream picks the compression method by this header — measured on deepseek: declare
#: zstd and it returns zstd, declare br and it returns br, and it **compresses even a 200 SSE
#: stream**, not just error responses. Yet this proxy must read and rewrite the response body
#: (SSE usage extraction, model-name injection, skill dedup, output filtering), so being
#: unable to decode means discarding the whole response **along with the upstream's real
#: status code**: when aiohttp's decoder is unavailable it raises `ContentEncodingError`
#: (inheriting `PayloadEncodingError` → `BadHttpMessage`, **code always 400**), which in
#: `ClientResponse.start()` is wrapped into `ClientResponseError(status=400, message='Can not
#: decode content-encoding: …')` and turned into a 502 by the except below — the `400` in the
#: error text is **not the upstream status code**; the client only sees an unexplained 502 and
#: retries repeatedly (the 2026-10-12 user report had exactly this shape).
#:
#: Only declare encodings aiohttp can actually decode in this venv: gzip/deflate go through
#: the stdlib zlib, br needs Brotli, zstd needs backports.zstd (aiohttp 3.14 only then
#: supports zstd, and only installs it via the speedups extra) — both are installed and
#: written into requirements.txt. Contract group 7 asserts "every declared encoding is
#: decodable by this process", so **writing one in on a machine without the library turns it
#: red** (that is exactly what it is meant to catch: declaring an undecodable encoding
#: amounts to discarding the whole response together with the real status code). Install the
#: library before adding a new encoding.
UPSTREAM_ACCEPT_ENCODING = "gzip, deflate, br, zstd"

logger = setup_logging("token_proxy", json_format=True)

#: Strong-reference pool for background tasks. asyncio's event loop holds only **weak
#: references** to create_task; under fire-and-forget usage a task may be garbage-collected
#: before it finishes, manifesting as randomly lost usage records (the official docs
#: explicitly require saving the reference). A completion callback releases it in time.
_BACKGROUND_TASKS: set = set()

#: Process-wide upstream connection pool: a per-request ClientSession discards the TCP/TLS
#: connection, paying a full handshake before the first token on every request.
_SESSION: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
        )
    return _SESSION


class _SharedSession:
    """Async context manager yielding the shared session, **closing nothing** on exit.

    It exists only so the forwarding body keeps its indentation — that line used to be
    `async with aiohttp.ClientSession() as session:`, building and tearing down a pool per
    request. Reading the global needs no lock: `_get_session` never awaits.
    """

    async def __aenter__(self) -> aiohttp.ClientSession:
        return _get_session()

    async def __aexit__(self, *_exc) -> bool:
        return False


async def _close_session(_app: web.Application) -> None:
    global _SESSION
    session, _SESSION = _SESSION, None
    if session is not None and not session.closed:
        await session.close()


def _load_providers() -> list[dict]:
    if not PROVIDERS_JSON:
        return []
    try:
        parsed = json.loads(PROVIDERS_JSON)
    except json.JSONDecodeError:
        logger.warning(
            json.dumps({"msg": "providers_json_invalid", "error": "bad JSON"})
        )
        return []

    # A provider missing keys cannot be routed to the expected model. If it is not reported
    # at load time, it only manifests as "the request is silently forwarded to providers[0]"
    # — which looks like a routing logic error but is actually a broken config surface.
    out: list[dict] = []
    for p in parsed:
        if not isinstance(p, dict) or not p.get("url"):
            continue
        absent = missing_keys(p)
        if absent:
            logger.warning(
                json.dumps(
                    {
                        "msg": "provider_config_missing",
                        "provider": p.get("name", ""),
                        "keys": absent,
                        "effect": "this provider may not route to the expected model",
                    }
                )
            )
        out.append(p)
    return out


PROVIDERS = _load_providers()

#: classifier acceleration allowlist (the sole source of client identity; the criterion comes from a configurable allowlist rather than a hard-coded client fingerprint)
CLIENT_ACCEL = load_client_accel(CLIENT_ACCEL_JSON)


def _resolve_provider(model: str) -> tuple[dict, str]:
    """Route a provider by body.model, returning (provider, upstream model name).

    - model matches some provider.models → that provider, upstream model uses the original value
    - no match → providers[0] as fallback, upstream model uses its models[0]
    - providers empty → compatible with the legacy single upstream (url + passed-through key)
    """
    for p in PROVIDERS:
        if model in p.get("models", []):
            return p, model
    if PROVIDERS:
        default = PROVIDERS[0]
        models = default.get("models") or ["unknown"]
        return default, models[0]
    return {
        "name": "legacy",
        "url": UPSTREAM_URL,
        "apiFormat": "anthropic",
        "apiKey": "",
    }, model


_logged_first_body: bool = False


#: Mapping prefixes of entry path → protocol format (compared after `_upstream_path`
#: normalization). **The entry path is the authoritative declaration of the protocol**, more
#: reliable than guessing from the request body: the heuristic necessarily falls to the
#: fallback branch on a "single-turn request with no system and no tools" (see
#: `_detect_api_format`), misjudging an OpenAI-protocol request as anthropic and performing a
#: wrong conversion.
ANTHROPIC_PATH_PREFIXES: tuple[str, ...] = ("v1/messages",)
OPENAI_PATH_PREFIXES: tuple[str, ...] = ("v1/chat/completions", "v1/completions")


def _upstream_path(request_path: str) -> str:
    """Client request path → upstream relative path (the part after provider.url).

    `/open/v1/messages` → `v1/messages`; `/v1/messages` → `v1/messages`;
    `/raw/v1/messages` → `v1/messages`. `/open` is the legacy compatibility entry (users
    who configured `ANTHROPIC_BASE_URL=.../open/v1`), `/v1` is the protocol-native entry,
    `/raw` is the transparent entry (no content rewrites); all three are normalized here
    and the subsequent logic faces only one form.
    """
    for entry in ("/open/", "/raw/"):
        if request_path.startswith(entry):
            return request_path[len(entry) :]
    return request_path.lstrip("/")


#: Client entry path → provider entry path, keyed by the provider's protocol. Only
#: unambiguous pairs are mapped; `v1/messages/count_tokens` has no OpenAI equivalent and
#: is forwarded unchanged (the upstream decides).
PROVIDER_PATH_MAP: dict[tuple[str, str], str] = {
    ("v1/messages", "openai"): "v1/chat/completions",
    ("v1/chat/completions", "anthropic"): "v1/messages",
    ("v1/completions", "anthropic"): "v1/messages",
}

#: Anthropic API requires this header; clients of the other protocol do not send it.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _provider_path(path: str, provider_format: str) -> str:
    """Map the client endpoint path to the provider's protocol endpoint."""
    return PROVIDER_PATH_MAP.get((path, provider_format), path)


def _api_format_from_path(upstream_path: str) -> str | None:
    """Determine the protocol format by the entry path; returns None when the path does not indicate the protocol (falls back to the heuristic)."""
    for prefix in ANTHROPIC_PATH_PREFIXES:
        if upstream_path.startswith(prefix):
            return "anthropic"
    for prefix in OPENAI_PATH_PREFIXES:
        if upstream_path.startswith(prefix):
            return "openai"
    return None
