"""Client fingerprint layer: identity allowlist + client-injected text/tag tables.

The proxy faces **generic agents** and does not presume any single client. This
module is the only entry point for "which client did this request come from, and
which client-injected artifacts does it carry":

- **Identity allowlist**: client identity is not a protocol fact and cannot be
  hard-coded into the proxy logic. Historically `proxy_protocol._is_claude_code`
  hard-coded Claude Code via a UA substring / private header, which left other
  clients unable to get classifier acceleration for requests of the **same
  structure** (stream=false + empty tools + messages ≤ 2) — when the upstream uses
  the default-thinking model the response takes 28-32s, hitting the client timeout
  and blocking the agent's key operations. Now it is a configurable allowlist,
  containing claude-code by default.
- **Client-injected text/tags**: continuation prompts, summary prefixes, and client
  tags vary by client version and differ across vendors, so they can only exist as
  a **configurable table**; on a miss we keep observation (`client_text_miss`),
  preventing new-client text from silently polluting `actual_user_input` and the
  session identifier anchor.

The config is serialized from config.yaml by Swift and passed in via env (the JSON
side is uniformly snake_case):

- `MODEL_PROXY_CLIENT_ACCEL`: `{"enabled": bool, "clients": [{"name",
  "user_agent_contains": [...], "header_present": [...]}]}`
- `MODEL_PROXY_CLIENT_TEXT`: `{"continuation_prefixes": [...],
  "summary_prefixes": [...], "injected_tags": [...]}`

When either is absent (not injected), the built-in defaults are used; the defaults
= historical behavior, hence **zero behavior change**.

The config is **incremental supplementation** rather than override: the built-in
defaults are always the baseline and config entries are appended (an entry with the
same client name overrides the default of the same name). Rationale: under override
semantics, writing just one field would silently clear the remaining defaults
(adding only a continuation prefix → both tag cleanup and summary prefixes stop
working, and summary text then leaks into `actual_user_input`) — precisely the
class of silent failure this layer exists to eliminate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("token_proxy", json_format=True)

# ---------------------------------------------------------------------------
# Contract surface: field agreement with Swift `TokenProxyManager.clientAccelJSON()/clientTextJSON()`
#
# At startup, report missing keys against the actual payload (the
# `client_config_missing` warning), and the contract script asserts that the
# Swift-serialized fields ⊇ this set.
# ---------------------------------------------------------------------------

ACCEL_CONTRACT_KEYS: tuple[str, ...] = ("enabled", "clients")
ACCEL_CLIENT_KEYS: tuple[str, ...] = ("name", "user_agent_contains", "header_present")
CLIENT_TEXT_CONTRACT_KEYS: tuple[str, ...] = (
    "continuation_prefixes",
    "summary_prefixes",
    "injected_tags",
)

# ---------------------------------------------------------------------------
# Built-in defaults (= behavior before the config surface was introduced, verbatim equivalent)
# ---------------------------------------------------------------------------

DEFAULT_ACCEL_CLIENTS: tuple[dict, ...] = (
    # The two source signals for Claude Code. **The historical implementation was OR**
    # (`x-anthropic-billing-header` present, or UA contains "claude", either one counts
    # as Claude Code) — split into two entries rather than one because of the two
    # conditions: the matcher internally is AND, and OR is expressed by multiple
    # entries; writing them as one would make clients carrying only a single signal
    # (e.g. versions that trimmed the billing header, claude-agent-sdk) silently lose
    # acceleration. The name carries the source so logs can tell which entry matched.
    {
        "name": "claude-code:billing-header",
        "header_present": ["x-anthropic-billing-header"],
    },
    {"name": "claude-code:user-agent", "user_agent_contains": ["claude"]},
)

DEFAULT_CONTINUATION_PREFIXES: tuple[str, ...] = (
    "This session is being continued from a previous conversation",
)
DEFAULT_SUMMARY_PREFIXES: tuple[str, ...] = ("Summary:",)
DEFAULT_INJECTED_TAGS: tuple[str, ...] = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
)


@dataclass(frozen=True)
class ClientMatcher:
    """Match conditions for a single client.

    Semantics: any of `user_agent_contains` matches, all of `header_present` are
    present; when both are declared it is **AND**; when both are empty it **does not
    match** (to prevent a misconfiguration from allowlisting all traffic).
    """

    name: str = ""
    user_agent_contains: tuple[str, ...] = field(default_factory=tuple)
    header_present: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ClientAccel:
    """Client allowlist for classifier acceleration. When `enabled=False` nothing is accelerated."""

    enabled: bool = True
    clients: tuple[ClientMatcher, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ClientText:
    """Each client's injected text and prefix/tag tables."""

    continuation_prefixes: tuple[str, ...] = DEFAULT_CONTINUATION_PREFIXES
    summary_prefixes: tuple[str, ...] = DEFAULT_SUMMARY_PREFIXES
    injected_tags: tuple[str, ...] = DEFAULT_INJECTED_TAGS


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _merge(defaults: tuple[str, ...], configured: tuple[str, ...]) -> tuple[str, ...]:
    """Defaults first, config after, de-duplicated preserving order (incremental supplementation semantics, see module docstring)."""
    out = list(defaults)
    seen = set(defaults)
    for item in configured:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _warn_missing(payload: dict, contract: tuple[str, ...], scope: str) -> None:
    missing = [k for k in contract if k not in payload]
    if missing:
        logger.warning(
            json.dumps(
                {
                    "msg": "client_config_missing",
                    "scope": scope,
                    "keys": missing,
                    "effect": "missing keys fall back to built-in defaults",
                }
            )
        )


def load_client_accel(raw: str) -> ClientAccel:
    """Parse MODEL_PROXY_CLIENT_ACCEL; on missing/invalid JSON fall back to the built-in default allowlist."""
    if not raw:
        return ClientAccel(enabled=True, clients=_builtin_matchers())
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            json.dumps(
                {
                    "msg": "client_accel_json_invalid",
                    "effect": "fall back to built-in defaults",
                }
            )
        )
        return ClientAccel(enabled=True, clients=_builtin_matchers())
    if not isinstance(parsed, dict):
        logger.warning(
            json.dumps(
                {
                    "msg": "client_accel_not_object",
                    "effect": "fall back to built-in defaults",
                }
            )
        )
        return ClientAccel(enabled=True, clients=_builtin_matchers())

    _warn_missing(parsed, ACCEL_CONTRACT_KEYS, "clientAccel")

    # Built-in defaults are the baseline; config entries override by same name and
    # append by different name (see the incremental-supplementation semantics in the
    # module docstring)
    matchers: list[ClientMatcher] = list(_builtin_matchers())
    for item in parsed.get("clients") or []:
        if not isinstance(item, dict):
            continue
        _warn_missing(item, ACCEL_CLIENT_KEYS, "clientAccel.clients")
        matcher = _to_matcher(item)
        for i, existing in enumerate(matchers):
            if matcher.name and existing.name == matcher.name:
                matchers[i] = matcher
                break
        else:
            matchers.append(matcher)
    # The only off switch is enabled; clients only grow and never shrink, so there is no
    # path that "mistakenly clears the allowlist"
    return ClientAccel(
        enabled=bool(parsed.get("enabled", True)), clients=tuple(matchers)
    )


def load_client_text(raw: str) -> ClientText:
    """Parse MODEL_PROXY_CLIENT_TEXT; on missing/invalid JSON fall back to the built-in default tables."""
    if not raw:
        return ClientText()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            json.dumps(
                {
                    "msg": "client_text_json_invalid",
                    "effect": "fall back to built-in defaults",
                }
            )
        )
        return ClientText()
    if not isinstance(parsed, dict):
        logger.warning(
            json.dumps(
                {
                    "msg": "client_text_not_object",
                    "effect": "fall back to built-in defaults",
                }
            )
        )
        return ClientText()

    _warn_missing(parsed, CLIENT_TEXT_CONTRACT_KEYS, "clientText")

    def _strs(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        value = parsed.get(key)
        if not isinstance(value, list):
            return fallback
        return _merge(fallback, tuple(str(v) for v in value if str(v)))

    return ClientText(
        continuation_prefixes=_strs(
            "continuation_prefixes", DEFAULT_CONTINUATION_PREFIXES
        ),
        summary_prefixes=_strs("summary_prefixes", DEFAULT_SUMMARY_PREFIXES),
        injected_tags=_strs("injected_tags", DEFAULT_INJECTED_TAGS),
    )


def _builtin_matchers() -> tuple[ClientMatcher, ...]:
    return tuple(_to_matcher(item) for item in DEFAULT_ACCEL_CLIENTS)


def _to_matcher(item: dict) -> ClientMatcher:
    def _tuple_of(key: str) -> tuple[str, ...]:
        value = item.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(str(v).strip() for v in value if str(v).strip())

    return ClientMatcher(
        name=str(item.get("name", "")),
        user_agent_contains=tuple(s.lower() for s in _tuple_of("user_agent_contains")),
        header_present=tuple(s.lower() for s in _tuple_of("header_present")),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _header(headers, name: str) -> str:
    """Case-insensitive request header lookup (works for both aiohttp's CIMultiDict and a plain dict)."""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is not None:
        return str(value)
    try:
        for key in headers.keys():
            if isinstance(key, str) and key.lower() == name.lower():
                return str(headers[key])
    except Exception:
        pass
    return ""


def matches_accel_client(headers, accel: ClientAccel) -> str | None:
    """The name of the matched client; returns None when unmatched or not enabled.

    Returns the name rather than a boolean — the logs must distinguish "which client
    matched"; otherwise troubleshooting can only see "not accelerated" and cannot
    tell whether the allowlist is unconfigured or the condition is malformed.
    """
    if not accel.enabled:
        return None
    ua = _header(headers, "user-agent").lower()
    for matcher in accel.clients:
        if not matcher.user_agent_contains and not matcher.header_present:
            continue
        if matcher.user_agent_contains and not any(
            s in ua for s in matcher.user_agent_contains
        ):
            continue
        if matcher.header_present and not all(
            _header(headers, h) for h in matcher.header_present
        ):
            continue
        return matcher.name or "unnamed"
    return None
