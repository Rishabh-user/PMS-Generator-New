"""Postgres-backed persistence for PMS-Agent chat sessions.

Public API (preserved across drivers):
    init()                                  — create table on startup
    list_sessions(user_id)
    get_session(user_id, session_id)
    upsert_session(user_id, session_id, title, blocks, message_count, last_preview)
    rename_session(user_id, session_id, title)
    delete_session(user_id, session_id)

The store is enabled when `DATABASE_URL` is configured in the
environment / .env. When unset, every API call raises
`SessionStoreUnavailableError` which the routes translate to HTTP 503 —
the frontend shows a "history sync off" banner rather than treating
it as a hard error.

Driver: psycopg 3 (modern, async-capable, but used synchronously here
since the chat endpoints are low-volume and FastAPI's threadpool covers
us). Connections are opened per-call; for the volume this serves
(per-user chat history, small JSON blobs) it's well under the cost of
pooling complexity.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from typing import Optional

from app.config import settings


logger = logging.getLogger(__name__)


class SessionStoreUnavailableError(RuntimeError):
    """Raised when DATABASE_URL is unset OR the database is unreachable.
    Routes catch this and return HTTP 503."""


def is_enabled() -> bool:
    url = (settings.database_url or "").strip()
    return bool(url) and url.startswith(("postgresql://", "postgres://"))


def normalised_url() -> str:
    """psycopg 3 accepts `postgresql://` but not `postgres://`. Normalise
    so older Render-style URIs work transparently."""
    url = settings.database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def connect():
    """Open a Postgres connection. Shared by every service that needs the
    PMS database (session store, admin browser, future readers).

    Lazy import of psycopg so the module still loads when the package
    isn't installed (e.g. during local dev without DATABASE_URL set)."""
    if not is_enabled():
        raise SessionStoreUnavailableError("DATABASE_URL not configured")
    try:
        import psycopg
        return psycopg.connect(normalised_url())
    except Exception as e:  # noqa: BLE001
        # Surface as our unavailable-error so routes return 503 instead
        # of a generic 500.
        raise SessionStoreUnavailableError(str(e)) from e


def init() -> None:
    """Create the table the first time the server boots. Idempotent.

    When DATABASE_URL is unset OR the database is unreachable, log a
    warning and return — the app still boots, and the session endpoints
    will return 503 until the DB becomes available."""
    if not is_enabled():
        logger.warning(
            "DATABASE_URL not configured — PMS-Agent chat history disabled. "
            "Set DATABASE_URL=postgresql://… in .env to enable session "
            "persistence."
        )
        return
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pms_agent_sessions (
                        id            TEXT NOT NULL,
                        user_id       TEXT NOT NULL,
                        title         TEXT NOT NULL DEFAULT 'New chat',
                        blocks_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        last_preview  TEXT NOT NULL DEFAULT '',
                        created_at    DOUBLE PRECISION NOT NULL,
                        updated_at    DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (id, user_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pms_agent_sessions_user_updated "
                    "ON pms_agent_sessions(user_id, updated_at DESC)"
                )
            conn.commit()
        logger.info("PMS-Agent session store initialised (Postgres).")
    except SessionStoreUnavailableError as e:
        # Already a typed error from connect() — re-log so operators see it.
        logger.warning(
            "Could not initialise PMS-Agent session store: %s. "
            "History endpoints will return 503 until the DB is reachable.",
            e,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error initialising session store: %s", e)


def _ts_iso(ts) -> str:
    """Normalise the timestamp shape Postgres hands back. The
    pms_agent_sessions table on the live Render Postgres has TIMESTAMPTZ
    columns (so reads return `datetime.datetime`), but earlier installs
    used DOUBLE PRECISION (Unix-epoch floats). Be permissive — accept
    either."""
    if ts is None:
        return ""
    if isinstance(ts, _dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return ts.astimezone(_dt.timezone.utc).replace(microsecond=0) \
                 .isoformat().replace("+00:00", "Z")
    # float / int Unix-epoch seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _now_for_column() -> _dt.datetime:
    """Timestamp value to pass through psycopg for the legacy
    pms_agent_sessions table (TIMESTAMPTZ columns). Postgres rejects
    a bare float for TIMESTAMPTZ, so use a tz-aware datetime."""
    return _dt.datetime.now(_dt.timezone.utc)


def list_sessions(user_id: str) -> list[dict]:
    """Sidebar history — sourced from `pms_agent_queries` grouped by
    `session_id` (the per-turn analytics log), not the legacy
    `pms_agent_sessions` blob.

    Derived columns:
      • title                — first user prompt in the session (≤60 chars)
      • message_count        — number of user turns in the session
      • created_at           — earliest query timestamp
      • updated_at           — latest query timestamp
      • last_message_preview — assistant's reply from the most recent turn

    Sessions with NULL `session_id` (older chat calls that didn't pass
    the id) are excluded — they'd group into one synthetic "session"
    which would be misleading.

    Chat replay (`GET /sessions/{id}`) still reads from
    `pms_agent_sessions.blocks_json` so match cards / Download / Save
    buttons survive."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    session_id,
                    (array_agg(prompt ORDER BY created_at ASC))[1]  AS first_prompt,
                    COUNT(*)                                        AS message_count,
                    (array_agg(reply  ORDER BY created_at DESC))[1] AS last_reply,
                    MIN(created_at)                                 AS first_at,
                    MAX(created_at)                                 AS last_at
                FROM   pms_agent_queries
                WHERE  user_id = %s AND session_id IS NOT NULL
                GROUP  BY session_id
                ORDER  BY MAX(created_at) DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    def _trim(s, n):
        if not s:
            return ""
        s = str(s).strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    out: list[dict] = []
    for sid, first_prompt, msg_count, last_reply, first_at, last_at in rows:
        out.append({
            "id":                   sid,
            "title":                _trim(first_prompt, 60) or "New chat",
            "created_at":           _ts_iso(first_at),
            "updated_at":           _ts_iso(last_at),
            "message_count":        int(msg_count or 0),
            "last_message_preview": _trim(last_reply, 120),
        })
    return out


def get_session(user_id: str, session_id: str) -> Optional[dict]:
    """Load a chat for replay.

    Preferred source: `pms_agent_sessions.blocks_json` — has the full
    response shape including match cards, slot state, suggested
    actions. This is what gives the click-to-replay UX its rich match
    cards / Save / Download / View Details buttons.

    Fallback source: `pms_agent_queries` — when the sessions blob is
    missing (older chats from before the TIMESTAMPTZ fix, or chats
    where the SPA's 400 ms auto-save never fired before navigation),
    we reconstruct minimal user + assistant text blocks per turn. The
    user sees the conversation as plain text — no match cards, no
    Save / Download — but that's strictly better than the silent 404
    that was happening before this fallback existed."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, blocks_json, message_count, last_preview,
                       created_at, updated_at
                FROM pms_agent_sessions
                WHERE user_id = %s AND id = %s
                """,
                (user_id, session_id),
            )
            row = cur.fetchone()
            if row:
                # psycopg returns JSONB columns as already-parsed Python objects.
                blocks = row[2] if isinstance(row[2], (list, dict)) else json.loads(row[2] or "[]")
                return {
                    "id":                   row[0],
                    "title":                row[1],
                    "created_at":           _ts_iso(row[5]),
                    "updated_at":           _ts_iso(row[6]),
                    "blocks":               blocks,
                    "message_count":        row[3],
                    "last_message_preview": row[4],
                }
            # No row in sessions blob — try to reconstruct from queries.
            cur.execute(
                """
                SELECT prompt, reply, intent, filters, field_suggestions,
                       created_at
                FROM pms_agent_queries
                WHERE user_id = %s AND session_id = %s
                ORDER BY created_at ASC
                """,
                (user_id, session_id),
            )
            qrows = cur.fetchall()

    if not qrows:
        return None  # truly nowhere to be found → 404 is honest

    blocks = []
    for prompt, reply, intent, filters, field_suggestions, _created_at in qrows:
        filters = filters or {}
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except Exception:  # noqa: BLE001
                filters = {}
        blocks.append({"kind": "user", "text": prompt})
        # Minimal assistant block: just the reply text. Match cards
        # can't be reconstructed because the queries table stores
        # only class codes, not per-class rating/material/CA — and
        # re-resolving each would be expensive at replay time.
        ratings = filters.get("ratings") or []
        materials = filters.get("materials") or []
        cas = filters.get("corrosion_allowances") or []
        services = filters.get("services") or []
        blocks.append({
            "kind": "assistant",
            "response": {
                "reply": reply or "",
                "interpreted": {
                    "piping_class":        None,
                    "rating":              (ratings or [None])[0],
                    "material":            (materials or [None])[0],
                    "corrosion_allowance": (cas or [None])[0],
                    "service":             ", ".join(services) if services else None,
                    "design_temp_c":        filters.get("design_temp_c"),
                    "design_pressure_barg": filters.get("design_pressure_barg"),
                    "intent":               intent or "unknown",
                },
                "matched_classes":     [],
                "suggested_action": {
                    "type":                 "none",
                    "piping_class":         None,
                    "material":             None,
                    "corrosion_allowance":  None,
                    "service":              None,
                    "design_pressure_barg": None,
                    "design_temp_c":        None,
                },
                "slots": {
                    "rating":              None,
                    "material":            None,
                    "corrosion_allowance": None,
                    "service":             None,
                    "missing":             [],
                    "complete":            False,
                },
                "field_suggestions":   field_suggestions or [],
                "available_values":    {},
                "allow_bulk_download": False,
            },
        })

    first_prompt = qrows[0][0] or ""
    last_reply = qrows[-1][1] or ""
    return {
        "id":                   session_id,
        "title":                (first_prompt[:60] + "…") if len(first_prompt) > 60 else (first_prompt or "Replayed chat"),
        "created_at":           _ts_iso(qrows[0][5]),
        "updated_at":           _ts_iso(qrows[-1][5]),
        "blocks":               blocks,
        "message_count":        len(qrows),
        "last_message_preview": last_reply[:120],
    }


def upsert_session(
    user_id: str,
    session_id: str,
    title: str,
    blocks: list,
    message_count: int,
    last_preview: str,
) -> None:
    now = _now_for_column()
    blocks_json = json.dumps(blocks)
    with connect() as conn:
        with conn.cursor() as cur:
            # Need to know whether the row exists to preserve created_at
            # on update — UPSERT-with-EXCLUDED for created_at would
            # overwrite the original timestamp.
            cur.execute(
                "SELECT created_at FROM pms_agent_sessions "
                "WHERE user_id = %s AND id = %s",
                (user_id, session_id),
            )
            existing = cur.fetchone()
            created_at = existing[0] if existing else now
            cur.execute(
                """
                INSERT INTO pms_agent_sessions
                    (id, user_id, title, blocks_json, message_count,
                     last_preview, created_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (id, user_id) DO UPDATE SET
                    title         = EXCLUDED.title,
                    blocks_json   = EXCLUDED.blocks_json,
                    message_count = EXCLUDED.message_count,
                    last_preview  = EXCLUDED.last_preview,
                    updated_at    = EXCLUDED.updated_at
                """,
                (
                    session_id, user_id, title, blocks_json,
                    message_count, last_preview, created_at, now,
                ),
            )
        conn.commit()


def rename_session(user_id: str, session_id: str, title: str) -> bool:
    now = _now_for_column()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pms_agent_sessions "
                "SET title = %s, updated_at = %s "
                "WHERE user_id = %s AND id = %s",
                (title, now, user_id, session_id),
            )
            rc = cur.rowcount
        conn.commit()
    return rc > 0


def delete_session(user_id: str, session_id: str) -> bool:
    """Drop a chat from BOTH tables — the chat-replay blob in
    `pms_agent_sessions` and the per-turn rows in `pms_agent_queries`.
    Without the queries-side delete the sidebar (which lists from
    queries grouped by session_id) would still show the chat after a
    sidebar "Delete"."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pms_agent_sessions "
                "WHERE user_id = %s AND id = %s",
                (user_id, session_id),
            )
            sessions_rc = cur.rowcount
            cur.execute(
                "DELETE FROM pms_agent_queries "
                "WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )
            queries_rc = cur.rowcount
        conn.commit()
    # Treat as "deleted something" if either table had a matching row.
    return (sessions_rc + queries_rc) > 0
