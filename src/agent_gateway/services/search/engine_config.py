"""Search engine registry: built-in specs merged with gateway config and env overrides.

Config shape (all keys optional):

    search:
      order:
        cjk: [sogou, google, so360, baidu, bing, marginalia, brave, duckduckgo]
        latin: [bing, google, marginalia, brave, duckduckgo, sogou, so360, baidu]
      engines:
        bing: {enabled: true, endpoint: "...", home: "...", parser: bing, min_interval: 1.2}
      providers:
        google:
          command: ["/path/to/browser-helper", "websearch", "--query", "{q}", "--limit", "{limit}"]
          timeout: 15

Env overrides (highest priority): `GATEWAY_SEARCH_ENGINES`, `GATEWAY_SEARCH_PROVIDERS`
and `GATEWAY_SEARCH_ORDER`, each holding the JSON object shown above.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass

from agent_gateway.core import config_loader

logger = logging.getLogger("gateway.search_config")


@dataclass(frozen=True)
class EngineSpec:
    endpoint: str
    home: str
    parser: str
    min_interval: float = 1.5
    warmup: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    command: tuple[str, ...]
    timeout: int = 15


DEFAULT_ENGINES: dict[str, EngineSpec] = {
    "bing": EngineSpec(
        "https://www.bing.com/search?q={q}&count=10",
        "https://www.bing.com/",
        "bing",
        1.2,
    ),
    "duckduckgo": EngineSpec(
        "https://html.duckduckgo.com/html/?q={q}",
        "https://html.duckduckgo.com/html/",
        "duckduckgo",
        2.5,
    ),
    "brave": EngineSpec(
        "https://search.brave.com/search?q={q}",
        "https://search.brave.com/",
        "brave",
        2.5,
    ),
    "sogou": EngineSpec(
        "https://www.sogou.com/web?query={q}", "https://www.sogou.com/", "sogou", 2.0
    ),
    "so360": EngineSpec(
        "https://www.so.com/s?q={q}", "https://www.so.com/", "so360", 1.5
    ),
    "baidu": EngineSpec(
        "https://www.baidu.com/s?wd={q}&rn=10",
        "https://www.baidu.com/",
        "baidu",
        2.0,
        True,
    ),
    "marginalia": EngineSpec(
        "https://old-search.marginalia.nu/search?query={q}",
        "https://old-search.marginalia.nu/",
        "marginalia",
        1.5,
    ),
}

DEFAULT_ORDER: dict[str, tuple[str, ...]] = {
    "cjk": (
        "sogou",
        "google",
        "so360",
        "baidu",
        "bing",
        "marginalia",
        "brave",
        "duckduckgo",
    ),
    "latin": (
        "bing",
        "google",
        "marginalia",
        "brave",
        "duckduckgo",
        "sogou",
        "so360",
        "baidu",
    ),
}


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _json_env(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        logger.warning(f"{name} is not valid JSON, ignored: {exc}")
        return {}
    return _as_dict(parsed)


def _merge_engines(base: dict[str, EngineSpec], overrides: dict) -> None:
    for name, raw in overrides.items():
        if not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            base.pop(str(name), None)
            continue
        current = base.get(str(name)) or EngineSpec("", "", str(name))
        base[str(name)] = EngineSpec(
            endpoint=str(raw.get("endpoint", current.endpoint)),
            home=str(raw.get("home", current.home)),
            parser=str(raw.get("parser", current.parser)),
            min_interval=float(raw.get("min_interval", current.min_interval)),
            warmup=bool(raw.get("warmup", current.warmup)),
        )


def _merge_providers(base: dict[str, ProviderSpec], overrides: dict) -> None:
    for name, raw in overrides.items():
        if not isinstance(raw, dict):
            continue
        command = raw.get("command", ())
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            base.pop(str(name), None)
            continue
        base[str(name)] = ProviderSpec(
            tuple(str(part) for part in command),
            int(raw.get("timeout", 15)),
        )


def _merge_order(base: dict[str, tuple[str, ...]], overrides: dict) -> None:
    for key in ("cjk", "latin"):
        value = overrides.get(key)
        if isinstance(value, list) and value:
            base[key] = tuple(str(item) for item in value)


def _available(
    order: tuple[str, ...], engines: dict, providers: dict
) -> tuple[str, ...]:
    return tuple(name for name in order if name in engines or name in providers)


def load() -> tuple[dict[str, EngineSpec], dict[str, ProviderSpec], dict[str, tuple]]:
    """Return `(engines, providers, order)` after config-file and env overrides."""
    engines = dict(DEFAULT_ENGINES)
    providers: dict[str, ProviderSpec] = {}
    order = dict(DEFAULT_ORDER)

    file_config = _as_dict(config_loader.value("search", {}))
    _merge_engines(engines, _as_dict(file_config.get("engines")))
    _merge_providers(providers, _as_dict(file_config.get("providers")))
    _merge_order(order, _as_dict(file_config.get("order")))

    _merge_engines(engines, _json_env("GATEWAY_SEARCH_ENGINES"))
    _merge_providers(providers, _json_env("GATEWAY_SEARCH_PROVIDERS"))
    _merge_order(order, _json_env("GATEWAY_SEARCH_ORDER"))

    resolved = {
        "cjk": _available(order["cjk"], engines, providers),
        "latin": _available(order["latin"], engines, providers),
    }
    return engines, providers, resolved
