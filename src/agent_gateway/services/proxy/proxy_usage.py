"""Token usage recording layer for token_proxy (facade).

Implementation lives in usage_constants / usage_db / usage_resolver /
usage_extract / usage_record. This module only re-exports the public surface.
"""

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


from agent_gateway.services.proxy.usage_constants import DB_PATH, TOOL_OUTPUT_DB_PATH
from agent_gateway.services.proxy.usage_db import (
    init_db,
    init_tool_output_db,
    record_gateway_log,
)
from agent_gateway.services.proxy.usage_extract import extract_tool_outputs
from agent_gateway.services.proxy.usage_record import record_tool_outputs, record_usage
