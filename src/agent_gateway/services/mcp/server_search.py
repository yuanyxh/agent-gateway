"""Project dependency-graph query + note semantic search (facade).

Implementation lives in search_constants / search_symbols / search_vectors /
search_notes_io / search_notes_cache / search_notes_query / project_tools.
This module only re-exports the public surface.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

import numpy as np

from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.synonyms import load_synonyms
from agent_gateway.core.tool_defaults import arg_default
from agent_gateway.services.mcp.server_embed import _get_shared_embedder, embed_query

from agent_gateway.services.mcp.search_constants import (
    DEFAULT_PROJECT_SEARCH_LIMIT,
    DEFAULT_PROJECT_STRUCTURE_LIMIT,
)
from agent_gateway.services.mcp.project_tools import (
    tool_project_search,
    tool_project_structure,
)
from agent_gateway.services.mcp.search_constants import _symbol_embeddings
from agent_gateway.services.mcp.search_notes_cache import _ensure_note_cache
from agent_gateway.services.mcp.search_notes_query import _query_project_notes
from agent_gateway.services.mcp.search_vectors import (
    _ensure_symbol_embeddings,
    _search_vectors,
)
