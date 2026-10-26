"""Usage/tool-output/gateway-log database initialization and logging."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from agent_gateway.core.db import conn as dbconn


from agent_gateway.services.proxy.usage_constants import (
    API_USAGE_RETENTION_DAYS,
    DB_DIR,
    DB_PATH,
    GATEWAY_LOG_RETENTION_DAYS,
    GATEWAY_LOG_SIZE_THRESHOLD,
    TOOL_OUTPUT_DB_PATH,
    TOOL_OUTPUT_RETENTION_DAYS,
)


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            request_type TEXT,
            context_summary TEXT,
            input_preview TEXT,
            input_message_count INTEGER,
            has_tool_result INTEGER DEFAULT 0,
            output_preview TEXT,
            output_type TEXT,
            stop_reason TEXT,
            response_valid INTEGER DEFAULT 1,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            api_format TEXT,
            request_level TEXT DEFAULT 'core',
            tool_name TEXT,
            parent_id INTEGER,
            actual_user_input TEXT,
            error_message TEXT,
            route_tier TEXT,
            route_reason TEXT,
            profile_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS usage_field_cache (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_path TEXT,
            output_path TEXT,
            cache_read_path TEXT,
            cache_write_path TEXT,
            updated_at TEXT,
            PRIMARY KEY (provider, model)
        );
        -- Gateway-layer log: records non-2xx responses and oversized requests (written by middleware).
        -- Distinguished from api_usage: api_usage records token usage of successful requests;
        -- gateway_log records rejected/failed/warning requests, resolving the troubleshooting
        -- blind spot where an aiohttp-layer 413 over-limit etc. prevents the handler from running,
        -- leaving no record at all.
        CREATE TABLE IF NOT EXISTS gateway_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            method TEXT,
            path TEXT,
            content_length INTEGER,
            status_code INTEGER,
            error_type TEXT,
            error_message TEXT,
            latency_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_log_timestamp ON gateway_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(timestamp);
        CREATE INDEX IF NOT EXISTS idx_api_usage_level ON api_usage(request_level);
    """)

    # Migration: add new columns to old tables (if absent)
    cursor = conn.execute("PRAGMA table_info(api_usage)")
    columns = [row[1] for row in cursor.fetchall()]
    if "request_level" not in columns:
        conn.execute(
            "ALTER TABLE api_usage ADD COLUMN request_level TEXT DEFAULT 'core'"
        )
        conn.execute("ALTER TABLE api_usage ADD COLUMN tool_name TEXT")
        conn.execute("ALTER TABLE api_usage ADD COLUMN parent_id INTEGER")
        conn.execute("ALTER TABLE api_usage ADD COLUMN actual_user_input TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_usage_level ON api_usage(request_level)"
        )
        # Backfill request_level from stop_reason
        conn.execute(
            "UPDATE api_usage SET request_level = 'process' WHERE stop_reason = 'tool_use'"
        )
        conn.commit()
    if "error_message" not in columns:
        conn.execute("ALTER TABLE api_usage ADD COLUMN error_message TEXT")
        conn.commit()

    # Migration: tiered-routing diagnostic columns (route_tier tier / route_reason decision
    # reason / profile_hash fingerprint of the conversation's agent type). It is the only means
    # to troubleshoot "why did this turn take this model" — the routing decision logs roll off
    # in token_proxy.log, and these two columns are the traceable history.
    if "route_tier" not in columns:
        conn.execute("ALTER TABLE api_usage ADD COLUMN route_tier TEXT")
        conn.execute("ALTER TABLE api_usage ADD COLUMN route_reason TEXT")
        conn.execute("ALTER TABLE api_usage ADD COLUMN profile_hash TEXT")
        conn.commit()
    # Migration: routing decision detail column. When a downgrade is blocked, record the
    # outside tool list; when upgrading, record the triggering signal name. The routing logs
    # roll off in token_proxy.log, and the detail column is the only persistent means to
    # troubleshoot "why was it not downgraded" — without it, the 3 hold_not_readonly return
    # points cannot be distinguished from the data side.
    if "route_detail" not in columns:
        conn.execute("ALTER TABLE api_usage ADD COLUMN route_detail TEXT")
        conn.commit()

    # Migration: remove three deprecated fields (session_id/project_path/estimated_cost_usd)
    # First DROP the indexes referencing the columns, then DROP COLUMN (SQLite constraint)
    if "session_id" in columns:
        conn.execute("DROP INDEX IF EXISTS idx_api_usage_session")
        conn.execute("ALTER TABLE api_usage DROP COLUMN session_id")
    if "project_path" in columns:
        conn.execute("DROP INDEX IF EXISTS idx_api_usage_project")
        conn.execute("ALTER TABLE api_usage DROP COLUMN project_path")
    if "estimated_cost_usd" in columns:
        conn.execute("ALTER TABLE api_usage DROP COLUMN estimated_cost_usd")
    conn.commit()

    # Fix history: a first-day interruption (stop_reason empty string '') was backfilled to
    # core by default; correct it to process. The current write logic maps empty stop_reason
    # to process, so new data will not produce this combination; idempotent and harmless.
    conn.execute(
        "UPDATE api_usage SET request_level = 'process' "
        "WHERE request_level = 'core' AND (stop_reason IS NULL OR stop_reason = '')"
    )
    conn.commit()

    # Three-month auto-cleanup: cutoff uses the same ISO format as timestamp, avoiding the
    # datetime('now') space format missing deletions on the boundary day
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=API_USAGE_RETENTION_DAYS)
    ).isoformat()
    conn.execute("DELETE FROM api_usage WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


def init_tool_output_db():
    """Initialize the independent tool_output_log DB (5-day retention).

    Column notes:
    - output_text_raw: raw output (the true value before proxy filtering), used for delta analysis
    - output_text: the content the proxy actually sent (equals raw when there is no filtering;
      diverges once filtering is live)
    - session_key: a hash of system+tools+first_user, aggregating total token consumption within one session
    """
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(TOOL_OUTPUT_DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_output_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_usage_id INTEGER,
            timestamp TEXT NOT NULL,
            tool_name TEXT,
            tool_call_id TEXT,
            output_text TEXT,
            output_text_raw TEXT,
            output_size INTEGER,
            output_lines INTEGER,
            is_error INTEGER,
            stop_reason TEXT,
            model TEXT,
            route_tier TEXT,
            session_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tool_output_timestamp ON tool_output_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_output_tool_name ON tool_output_log(tool_name);
    """)

    # Migration: add new columns to the old DB (idempotent). Column names are hard-coded literals, not external input.
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(tool_output_log)").fetchall()
    }
    for col in ("output_text_raw", "session_key"):
        if col not in existing:
            conn.execute(f"ALTER TABLE tool_output_log ADD COLUMN {col} TEXT")

    # The session_key index must be created **after** the migration: the old DB must
    # first gain that column, otherwise "CREATE INDEX referencing a non-existent column"
    # makes initialization raise directly and the proxy fails to start.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_output_session ON tool_output_log(session_key)"
    )
    conn.commit()

    # Five-day auto-cleanup
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=TOOL_OUTPUT_RETENTION_DAYS)
    ).isoformat()
    conn.execute("DELETE FROM tool_output_log WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


# Gateway-layer log: middleware uniformly records rejected/failed/oversized requests,
# covering api_usage's troubleshooting blind spot. api_usage only records token usage of
# successful requests; when an aiohttp-layer 413 over-limit etc. prevents the handler from
# running, nothing is persisted.
_gateway_log_write_count = 0


def record_gateway_log(
    method, path, content_length, status_code, error_type, error_message, latency_ms
):
    """Write the gateway log: non-2xx responses + oversized request bodies. Retained 90 days; expired data is deleted every 200 writes."""
    global _gateway_log_write_count
    if status_code < 400 and (content_length or 0) < GATEWAY_LOG_SIZE_THRESHOLD:
        return
    try:
        with dbconn(DB_PATH) as c:
            c.execute(
                "INSERT INTO gateway_log (timestamp, method, path, content_length, status_code, "
                "error_type, error_message, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    method,
                    path,
                    content_length,
                    status_code,
                    error_type,
                    error_message,
                    latency_ms,
                ),
            )
            _gateway_log_write_count += 1
            if _gateway_log_write_count % 200 == 0:
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=GATEWAY_LOG_RETENTION_DAYS)
                ).isoformat()
                c.execute("DELETE FROM gateway_log WHERE timestamp < ?", (cutoff,))
    except Exception:
        # A gateway-log failure does not affect the main flow
        pass
