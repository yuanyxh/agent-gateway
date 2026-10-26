#!/usr/bin/env python3
"""知识面板的 schema 所有者与全部数据访问。

DB 位置 = `config.data_dir()/knowledge.sqlite`（即 `~/.config/utility/`）。该 env 由 Swift 的
两个注入点（`baseEnvironment()` / `taskEnvironmentOverrides()`）都注成同一个 AppPaths.configDir，
故 Python 与 Swift 天然同源，不新增路径 env。

**建表与迁移只在这里发生**。Swift 侧（`Sources/Knowledge/KnowledgeStore.swift`）只做裸 SQL 读写，
绝不建表——表由 Python 拥有，两边各建一份 schema 必然漂移。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_gateway.core import config
from agent_gateway.core.db import conn as dbconn

WAL = "WAL"

# 卡片状态。`pool` 在池中待显示；其余两种都表示「已出池」，区别只在去向。
ST_POOL = "pool"
ST_LIKED = "liked"
ST_DISLIKED = "disliked"

KIND_TOPIC = "topic"


def db_path() -> str:
    base = config.data_dir() or str(Path.home() / ".config" / "utility")
    return str(Path(base) / "knowledge.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> str:
    """Create tables, run idempotent migrations, enable WAL. Safe to call repeatedly."""
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'principle',
                status TEXT NOT NULL DEFAULT 'active',
                position INTEGER NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT '',
                exclude TEXT NOT NULL DEFAULT '',
                fail_streak INTEGER NOT NULL DEFAULT 0,
                last_advanced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_topics_name ON topics(lower(name));
            CREATE INDEX IF NOT EXISTS idx_topics_pick ON topics(status, position);

            CREATE TABLE IF NOT EXISTS concepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                why_now TEXT NOT NULL DEFAULT '',
                seq INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'done',
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_concepts_topic_seq ON concepts(topic_id, seq);

            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                theme TEXT NOT NULL DEFAULT '',
                topic_kind TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                evidence_urls TEXT NOT NULL DEFAULT '[]',
                topic_id INTEGER,
                concept_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pool',
                shown_count INTEGER NOT NULL DEFAULT 0,
                last_shown_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cards_topic ON cards(topic_id);
            CREATE INDEX IF NOT EXISTS idx_cards_pool
                ON cards(kind, last_shown_at) WHERE status = 'pool';
        """)
        conn.commit()
        # 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)")}
        for col, ddl in (
            ("scope", "ALTER TABLE topics ADD COLUMN scope TEXT NOT NULL DEFAULT ''"),
            (
                "exclude",
                "ALTER TABLE topics ADD COLUMN exclude TEXT NOT NULL DEFAULT ''",
            ),
        ):
            if col not in cols:
                conn.execute(ddl)
        conn.commit()
        # 两侧（Python 子进程与 Swift 面板）同时打开同一个文件，WAL + busy timeout 都必须有，
        # 只设一侧会回落到 delete 模式并互相阻塞。
        conn.execute(f"PRAGMA journal_mode={WAL}")
        conn.commit()
    finally:
        conn.close()
    return path


# ---------- 读写 ----------


def pool_count(kind: str) -> int:
    with dbconn(db_path()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE kind = ? AND status = ?", (kind, ST_POOL)
        ).fetchone()
    return row[0] if row else 0


def unseen_count(kind: str) -> int:
    """从未展示过的池中卡数量，与 Swift `KnowledgeStore.unseenCount` 同口径。

    补货判据只能用「未展示」：旧卡即使允许复习，也不能掩盖需要补新卡的事实。
    """
    with dbconn(db_path()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE kind = ? AND status = ? AND shown_count = 0",
            (kind, ST_POOL),
        ).fetchone()
    return row[0] if row else 0


def next_card(
    kind: str,
    excluding_topic: int | None = None,
    show_reviewed: bool = False,
    max_shown: int = 2,
) -> dict | None:
    """取一张池中卡片并累加 shown_count。读与写在同一事务内，避免自锁。

    默认只取从未展示的卡；`show_reviewed=True` 时才回退到旧卡，且旧卡仍按
    最久远优先。复习上限由 `max_shown` 控制。
    """
    sql = (
        "SELECT id, kind, title, subtitle, theme, topic_kind, body, evidence,"
        " evidence_urls, topic_id, concept_id FROM cards"
        " WHERE kind = ? AND status = ?"
    )
    args: list = [kind, ST_POOL]
    if excluding_topic is not None:
        sql += " AND (topic_id IS NULL OR topic_id != ?)"
        args.append(excluding_topic)
    if show_reviewed:
        sql += " AND shown_count < ?"
        args.append(max(1, max_shown))
        sql += " ORDER BY (shown_count > 0) ASC, last_shown_at ASC, id ASC LIMIT 1"
    else:
        sql += " AND shown_count = 0 ORDER BY id ASC LIMIT 1"
    with dbconn(db_path()) as conn:
        row = conn.execute(sql, args).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute(sql, args).description]
        card = dict(zip(cols, row))
        conn.execute(
            "UPDATE cards SET shown_count = shown_count + 1, last_shown_at = ? WHERE id = ?",
            (_now(), card["id"]),
        )
    return card


def insert_card(
    *,
    kind: str,
    title: str,
    body: str,
    subtitle: str = "",
    theme: str = "",
    topic_kind: str = "",
    evidence: str = "",
    evidence_urls: list | None = None,
    topic_id: int | None = None,
    concept_id: int | None = None,
) -> int:
    with dbconn(db_path()) as conn:
        cur = conn.execute(
            "INSERT INTO cards (kind, title, subtitle, theme, topic_kind, body, evidence,"
            " evidence_urls, topic_id, concept_id, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                title,
                subtitle,
                theme,
                topic_kind,
                body,
                evidence,
                json.dumps(evidence_urls or [], ensure_ascii=False),
                topic_id,
                concept_id,
                ST_POOL,
                _now(),
            ),
        )
        return cur.lastrowid


def mark_status(card_id: int, status: str) -> None:
    with dbconn(db_path()) as conn:
        conn.execute("UPDATE cards SET status = ? WHERE id = ?", (status, card_id))


def prune_expired(days: int) -> int:
    """Delete cards older than `days`; liked cards are user favourites and kept. days <= 0 disables pruning."""
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    with dbconn(db_path()) as conn:
        cur = conn.execute(
            "DELETE FROM cards WHERE created_at <> '' AND created_at < ? AND status != ?",
            (cutoff, ST_LIKED),
        )
        return cur.rowcount


def disliked_titles(topic_id: int, limit: int = 5) -> list[str]:
    """Disliked card titles (newest first) used as negative feedback on the generation side."""
    with dbconn(db_path()) as conn:
        rows = conn.execute(
            "SELECT title FROM cards WHERE topic_id = ? AND status = ?"
            " ORDER BY id DESC LIMIT ?",
            (topic_id, ST_DISLIKED, limit),
        ).fetchall()
    return [r[0] for r in rows]


# ---------- 主题 ----------


def next_topic() -> dict | None:
    with dbconn(db_path()) as conn:
        row = conn.execute(
            "SELECT id, name, kind, fail_streak, scope, exclude FROM topics"
            " WHERE status = 'active' ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "kind": row[2],
        "fail_streak": row[3],
        "scope": row[4],
        "exclude": row[5],
    }


def next_concept_seq(topic_id: int) -> int:
    with dbconn(db_path()) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM concepts WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
    return row[0] if row else 0


def concepts_of(topic_id: int) -> list[dict]:
    """该主题**全部**概念行（不看 status）按 seq。

    这是喂给 `knowledge_expand` 的 `{done}`：「展开过就算推过」——严格单调前进，
    不可能重问同一个概念，进程崩溃时未落库的概念下轮自然重问（自愈）。
    """
    with dbconn(db_path()) as conn:
        rows = conn.execute(
            "SELECT name, why_now, seq, status FROM concepts WHERE topic_id = ? ORDER BY seq ASC",
            (topic_id,),
        ).fetchall()
    return [{"name": r[0], "why_now": r[1], "seq": r[2], "status": r[3]} for r in rows]


def insert_concept(
    topic_id: int, name: str, why_now: str, seq: int, status: str = "done"
) -> int:
    with dbconn(db_path()) as conn:
        cur = conn.execute(
            "INSERT INTO concepts (topic_id, name, why_now, seq, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (topic_id, name, why_now, seq, status, _now()),
        )
        return cur.lastrowid


def commit_card(
    *,
    kind: str,
    theme: str,
    title: str,
    body: str,
    evidence: str,
    evidence_urls: list,
    topic_kind: str = "",
    subtitle: str = "",
    topic_id: int | None = None,
    why_now: str = "",
    concept_status: str = "done",
) -> int:
    """cards +（主题化时）concepts 同事务写入，返回 card_id。

    概念行必须与卡片同事务：只写其一会在崩溃时留下「概念已消费但没卡片」或
    「卡片存在但下一轮重问同一概念」的不一致。
    """
    now = _now()
    with dbconn(db_path()) as conn:
        concept_id = None
        if topic_id is not None:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM concepts WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO concepts (topic_id, name, why_now, seq, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (topic_id, title, why_now, seq, concept_status, now),
            )
            concept_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO cards (kind, title, subtitle, theme, topic_kind, body, evidence,"
            " evidence_urls, topic_id, concept_id, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                title,
                subtitle or why_now,
                theme,
                topic_kind,
                body,
                evidence,
                json.dumps(evidence_urls, ensure_ascii=False),
                topic_id,
                concept_id,
                ST_POOL,
                now,
            ),
        )
        return cur.lastrowid


def bump_topic_fail(topic_id: int, max_streak: int) -> int:
    """Add one to the no-output streak; mark done at the threshold. Returns the new streak."""
    now = _now()
    with dbconn(db_path()) as conn:
        conn.execute(
            "UPDATE topics SET fail_streak = fail_streak + 1, updated_at = ? WHERE id = ?",
            (now, topic_id),
        )
        row = conn.execute(
            "SELECT fail_streak FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        streak = row[0] if row else 0
        if streak >= max_streak:
            conn.execute(
                "UPDATE topics SET status = 'done', updated_at = ? WHERE id = ?",
                (now, topic_id),
            )
    return streak


def reset_topic_fail(topic_id: int) -> None:
    now = _now()
    with dbconn(db_path()) as conn:
        conn.execute(
            "UPDATE topics SET fail_streak = 0, last_advanced_at = ?, updated_at = ? WHERE id = ?",
            (now, now, topic_id),
        )


def mark_topic_done(topic_id: int) -> None:
    now = _now()
    with dbconn(db_path()) as conn:
        conn.execute(
            "UPDATE topics SET status = 'done', fail_streak = 0, updated_at = ?"
            " WHERE id = ?",
            (now, topic_id),
        )


# ---------- 种子 ----------


def seed_from_config() -> int:
    """Write configured topic seeds into the DB (idempotent, existing rows skipped). Returns the added count."""
    now = _now()
    added_topics = 0
    topics = config.knowledge_topics()
    with dbconn(db_path()) as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM topics").fetchone()[
            0
        ]
        for t in topics:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name", "")).strip()
            if not name:
                continue
            pos += 1
            scope = str(t.get("scope", "")).strip()
            exclude = str(t.get("exclude", "")).strip()
            # 唯一索引在 lower(name) 上，INSERT OR IGNORE 对已存在主题幂等
            cur = conn.execute(
                "INSERT OR IGNORE INTO topics"
                " (name, kind, status, position, scope, exclude, created_at, updated_at)"
                " VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                (name, t.get("kind", "principle"), pos, scope, exclude, now, now),
            )
            added_topics += cur.rowcount if cur.rowcount > 0 else 0
            # 已存在的主题也要同步 scope：配置是种子主题边界的唯一来源，
            # 否则给老主题补 scope 永远不生效。
            if cur.rowcount == 0 and ("scope" in t or "exclude" in t):
                conn.execute(
                    "UPDATE topics SET scope = ?, exclude = ?, updated_at = ?"
                    " WHERE lower(name) = lower(?)",
                    (scope, exclude, now, name),
                )
    return added_topics
