"""Per-turn query log for the PMS AI Agent chat.

Each row records one user prompt → assistant reply, capturing:
  • the interpreted filters Claude pulled out of the prompt
  • the matched piping classes (codes only — full data lives in the
    chat session blob)
  • Claude token usage + end-to-end latency, so cost / perf can be
    monitored from SQL
  • intent classification (generate / list / info / unknown)
  • any error / 0-match outcome for fast triage of broken queries

This complements `pms_agent_sessions` (which stores the entire chat
conversation as a JSONB blob): the queries table is the normalised
per-turn view, easier to slice for analytics. Both tables write on
every chat call.

Writes are best-effort: a DB outage logs a warning but never breaks
the chat response — the user's chat must keep working even if the
analytics pipeline is down.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from app.services import session_store
from app.services.session_store import SessionStoreUnavailableError

logger = logging.getLogger(__name__)


def init() -> None:
    """Create the pms_agent_queries table on app startup. Idempotent.
    Non-fatal when DATABASE_URL is unset or the DB is unreachable —
    queries simply won't be logged until it's back."""
    if not session_store.is_enabled():
        logger.warning(
            "DATABASE_URL not configured — pms_agent_queries table will not be created."
        )
        return
    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pms_agent_queries (
                        id                  SERIAL PRIMARY KEY,
                        user_id             TEXT NOT NULL,
                        session_id          TEXT,
                        prompt              TEXT NOT NULL,
                        reply               TEXT NOT NULL DEFAULT '',
                        intent              TEXT,
                        filters             JSONB NOT NULL DEFAULT '{}'::jsonb,
                        matched_class_codes TEXT[] NOT NULL DEFAULT '{}',
                        match_count         INTEGER NOT NULL DEFAULT 0,
                        field_suggestions   JSONB NOT NULL DEFAULT '[]'::jsonb,
                        model               TEXT,
                        tokens_in           INTEGER,
                        tokens_out          INTEGER,
                        latency_ms          INTEGER,
                        error               TEXT,
                        created_at          DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pms_agent_queries_user_created "
                    "ON pms_agent_queries(user_id, created_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pms_agent_queries_session "
                    "ON pms_agent_queries(session_id, created_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pms_agent_queries_intent "
                    "ON pms_agent_queries(intent)"
                )
            conn.commit()
        logger.info("pms_agent_queries table initialised.")
    except SessionStoreUnavailableError as e:
        logger.warning(
            "Could not initialise pms_agent_queries: %s. "
            "Chat queries will not be logged until the DB is reachable.",
            e,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error initialising pms_agent_queries: %s", e)


def log(
    *,
    user_id: str,
    session_id: Optional[str],
    prompt: str,
    response: Optional[dict],
    metrics: Optional[dict],
    error: Optional[str] = None,
) -> None:
    """Best-effort insert of one chat turn. Never raises — DB outages
    are logged and swallowed so the caller (the chat route) can still
    return its happy-path response to the user."""
    if not session_store.is_enabled():
        return

    metrics = metrics or {}
    response = response or {}
    interpreted = response.get("interpreted") or {}
    matches = response.get("matched_classes") or []

    filters = {
        "ratings":              (response.get("slots") or {}).get("ratings"),
        "materials":            (response.get("slots") or {}).get("materials"),
        "corrosion_allowances": (response.get("slots") or {}).get("corrosion_allowances"),
        "services":             (response.get("slots") or {}).get("services"),
        "exclusions":           (response.get("slots") or {}).get("exclusions"),
        "rating_min":           (response.get("slots") or {}).get("rating_min"),
        "rating_max":           (response.get("slots") or {}).get("rating_max"),
        "design_pressure_barg": interpreted.get("design_pressure_barg"),
        "design_temp_c":        interpreted.get("design_temp_c"),
    }
    matched_class_codes = [m.get("piping_class") for m in matches if m.get("piping_class")]

    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pms_agent_queries
                        (user_id, session_id, prompt, reply, intent, filters,
                         matched_class_codes, match_count, field_suggestions,
                         model, tokens_in, tokens_out, latency_ms, error,
                         created_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb,
                            %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        session_id,
                        prompt,
                        (response.get("reply") or "")[:8000],  # safety truncate
                        interpreted.get("intent"),
                        json.dumps(filters),
                        matched_class_codes,
                        len(matches),
                        json.dumps(response.get("field_suggestions") or []),
                        metrics.get("model"),
                        metrics.get("tokens_in"),
                        metrics.get("tokens_out"),
                        metrics.get("latency_ms"),
                        error,
                        time.time(),
                    ),
                )
            conn.commit()
    except SessionStoreUnavailableError as e:
        logger.warning("pms_agent_queries log skipped — DB unavailable: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error logging chat turn: %s", e)
