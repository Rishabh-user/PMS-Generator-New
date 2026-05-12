"""SQLite-backed persistence for PMS-Agent chat sessions.

Replaces what the old pms-generator backend did with Postgres. SQLite keeps
deploys simple (no external service) — for the volume this endpoint sees
(per-user chat history for an internal engineering tool) it's more than
enough.

Schema: one table, one row per session, per user. The X-User-Id header from
the frontend scopes the rows. No server-side auth — the same trust model the
old backend used.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import settings


_DB_PATH = settings.data_dir / "pms_agent_sessions.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    """Create the table the first time the server starts."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pms_agent_sessions (
                id            TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                title         TEXT NOT NULL DEFAULT 'New chat',
                blocks_json   TEXT NOT NULL DEFAULT '[]',
                message_count INTEGER NOT NULL DEFAULT 0,
                last_preview  TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                PRIMARY KEY (id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_updated "
            "ON pms_agent_sessions(user_id, updated_at DESC)"
        )


def _ts_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def list_sessions(user_id: str) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, message_count, last_preview, created_at, updated_at "
            "FROM pms_agent_sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_at": _ts_iso(r["created_at"]),
            "updated_at": _ts_iso(r["updated_at"]),
            "message_count": r["message_count"],
            "last_message_preview": r["last_preview"],
        }
        for r in rows
    ]


def get_session(user_id: str, session_id: str) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pms_agent_sessions WHERE user_id = ? AND id = ?",
            (user_id, session_id),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": _ts_iso(row["created_at"]),
        "updated_at": _ts_iso(row["updated_at"]),
        "blocks": json.loads(row["blocks_json"]),
        "message_count": row["message_count"],
        "last_message_preview": row["last_preview"],
    }


def upsert_session(
    user_id: str,
    session_id: str,
    title: str,
    blocks: list,
    message_count: int,
    last_preview: str,
) -> None:
    now = time.time()
    blocks_json = json.dumps(blocks)
    with _lock, _connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM pms_agent_sessions WHERE user_id = ? AND id = ?",
            (user_id, session_id),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """
            INSERT INTO pms_agent_sessions
                (id, user_id, title, blocks_json, message_count, last_preview, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, user_id) DO UPDATE SET
                title         = excluded.title,
                blocks_json   = excluded.blocks_json,
                message_count = excluded.message_count,
                last_preview  = excluded.last_preview,
                updated_at    = excluded.updated_at
            """,
            (
                session_id, user_id, title, blocks_json,
                message_count, last_preview, created_at, now,
            ),
        )


def rename_session(user_id: str, session_id: str, title: str) -> bool:
    now = time.time()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE pms_agent_sessions SET title = ?, updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (title, now, user_id, session_id),
        )
        return cur.rowcount > 0


def delete_session(user_id: str, session_id: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM pms_agent_sessions WHERE user_id = ? AND id = ?",
            (user_id, session_id),
        )
        return cur.rowcount > 0
