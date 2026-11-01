"""Language adapters for the graph indexer.

A missing grammar disables only its own language; the service still answers
ping and indexes the remaining languages.
"""

from __future__ import annotations

import importlib
import logging

from agent_gateway.services.graph.constants import EXTENSIONS as EXTENSIONS

logger = logging.getLogger("graph")

_MODULES = {
    "python": "agent_gateway.services.graph.parsers.python",
    "swift": "agent_gateway.services.graph.parsers.swift",
    "javascript": "agent_gateway.services.graph.parsers.typescript",
    "typescript": "agent_gateway.services.graph.parsers.typescript",
    "java": "agent_gateway.services.graph.parsers.java",
    "kotlin": "agent_gateway.services.graph.parsers.kotlin",
    "vue": "agent_gateway.services.graph.parsers.vue",
    "objc": "agent_gateway.services.graph.parsers.objc",
}

PARSERS = {}
for _language, _module_name in _MODULES.items():
    try:
        PARSERS[_language] = importlib.import_module(_module_name).parse
    except Exception as exc:  # noqa: BLE001 - one missing grammar must not kill the rest
        logger.warning("parser unavailable: %s (%s)", _language, exc)
