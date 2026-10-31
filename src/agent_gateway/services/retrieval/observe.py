"""Trace wrappers for retrievers that keep their own algorithm.

`project_search` and `project_notes` are not on the pipeline yet; wrapping them
keeps trace coverage at 100% so both can be compared before migration.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

from agent_gateway.services.retrieval import trace as trace_mod
from agent_gateway.services.retrieval.text.analyzer import detect_language


def record_call(
    tool: str,
    query: str,
    status: str,
    count: int,
    latency_ms: float,
    *,
    layers: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    trace_id = trace_mod.new_trace_id()
    payload = {
        "trace_id": trace_id,
        "tool": tool,
        "profile": tool,
        "query": query,
        "query_lang": detect_language(query),
        "status": status,
        "count": count,
        "layers": layers or [],
        "latency_ms": {"total": round(latency_ms, 2)},
    }
    if extra:
        payload.update(extra)
    trace_mod.record(payload)
    return trace_id


def traced(tool: str, query_arg: int | str = "query") -> Callable:
    """Record one trace line per call; always returns the wrapped result."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            result = fn(*args, **kwargs)
            try:
                if "query" in kwargs:
                    query = kwargs["query"]
                elif isinstance(query_arg, int):
                    query = args[query_arg] if len(args) > query_arg else ""
                else:
                    query = kwargs.get(query_arg, "")
                if isinstance(result, dict):
                    record_call(
                        tool,
                        str(query or ""),
                        str(
                            result.get("status")
                            or ("ok" if result.get("results") else "no_match")
                        ),
                        int(result.get("total") or result.get("count") or 0),
                        (time.perf_counter() - started) * 1000,
                        layers=result.get("layers"),
                        extra={"error": result.get("error")}
                        if result.get("error")
                        else None,
                    )
            except Exception:  # noqa: BLE001 - tracing never breaks a call
                pass
            return result

        return wrapper

    return decorator
