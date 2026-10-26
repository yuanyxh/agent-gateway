"""Usage recording configuration and shared logger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from agent_gateway.llm import client as llm_client
from agent_gateway.core.config import data_dir
from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.proxy.proxy_client import load_client_text
from agent_gateway.services.proxy.proxy_normalize import _result_text as result_text
from agent_gateway.services.proxy.proxy_protocol import strip_injected_rules

# ---------------------------------------------------------------------------
# Configuration (identical to token_proxy.py before the split)
# ---------------------------------------------------------------------------

DB_DIR = data_dir()
DB_PATH = os.path.join(DB_DIR, "token_usage.sqlite")
# Independent DB for tool_output_log (same directory as token_usage.sqlite, managed separately)
TOOL_OUTPUT_DB_PATH = os.path.join(DB_DIR, "tool_output.sqlite")
# api_usage retention days (about 3 months); expired records are cleaned at startup
API_USAGE_RETENTION_DAYS = 90
# tool_output_log retention days (short-term, for shape analysis)
TOOL_OUTPUT_RETENTION_DAYS = 5
# gateway_log recording scope and retention: non-2xx + request body >=512KB warnings, retained 90 days
GATEWAY_LOG_SIZE_THRESHOLD = 512 * 1024
GATEWAY_LOG_RETENTION_DAYS = 90
# LLM configuration (for usage field path resolution, see UsageFieldResolver._ask_model) is now
# uniformly read by llm_client. The former three aliases LLM_API_URL/LLM_API_KEY/LLM_MODEL
# amounted to reading the same config a second time, and the "disabled when unconfigured" guard
# was scattered here — when the config is renamed here it would not error, only silently disable
# resolution.

# Client-injected text/tags (continuation prompts, summary prefixes, client tags): each client's
# values differ and change with versions, so they exist as a **configurable table**
# (config.yaml modelProxy.clientText → MODEL_PROXY_CLIENT_TEXT); when absent the built-in
# defaults of proxy_client are used (= behavior before the config surface, zero behavior change).
CLIENT_TEXT = load_client_text(os.environ.get("MODEL_PROXY_CLIENT_TEXT", ""))

logger = setup_logging("token_proxy", json_format=True)
