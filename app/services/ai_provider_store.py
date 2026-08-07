"""Postgres-backed persistence for saved AI provider credentials
(Anthropic / OpenAI / OpenAI-compatible), managed from /admin/ai-settings.

Multiple rows can exist at once — an admin can save several providers
and switch which one is active without re-entering keys. Exactly one
row may have is_active=True; enforced here via an explicit
deactivate-then-activate transaction (see `activate_config`).

Reuses the same Postgres connection helper as the PMS-Agent session
store (`session_store.connect`) — same DATABASE_URL, same psycopg
driver, same "raise SessionStoreUnavailableError, routes turn it into
HTTP 503" convention, just a different table. When DATABASE_URL is
unset the settings page can't save/list providers — the app keeps
working off the .env ANTHROPIC_API_KEY fallback (see
app/services/ai_provider.py resolve_active_provider()).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services.session_store import SessionStoreUnavailableError, connect, is_enabled


logger = logging.getLogger(__name__)


def init() -> None:
    """Create the table the first time the server boots. Idempotent.

    When DATABASE_URL is unset or unreachable, log a warning and
    return — /admin/ai-settings will report the DB as unavailable
    until it's back, same as the chat session store."""
    if not is_enabled():
        logger.warning(
            "DATABASE_URL not configured — AI Settings admin page can't "
            "save/list providers. Set DATABASE_URL=postgresql://… in .env "
            "to enable it."
        )
        return
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_provider_configs (
                        id                 SERIAL PRIMARY KEY,
                        provider           TEXT NOT NULL,
                        label              TEXT NOT NULL,
                        api_key_encrypted  TEXT NOT NULL,
                        model              TEXT,
                        base_url           TEXT,
                        is_active          BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ai_provider_configs_active "
                    "ON ai_provider_configs(is_active) WHERE is_active"
                )
            conn.commit()
        logger.info("AI provider config store initialised (Postgres).")
    except SessionStoreUnavailableError as e:
        logger.warning(
            "Could not initialise AI provider config store: %s. "
            "/admin/ai-settings will report the DB unavailable until "
            "it's reachable.",
            e,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error initialising AI provider config store: %s", e)


_COLUMNS = "id, provider, label, api_key_encrypted, model, base_url, is_active, created_at, updated_at"


def _row_to_dict(row) -> dict:
    (id_, provider, label, api_key_encrypted, model, base_url,
     is_active, created_at, updated_at) = row
    return {
        "id": id_,
        "provider": provider,
        "label": label,
        "api_key_encrypted": api_key_encrypted,
        "model": model,
        "base_url": base_url,
        "is_active": bool(is_active),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def list_configs() -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM ai_provider_configs ORDER BY created_at ASC")
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_config(config_id: int) -> Optional[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM ai_provider_configs WHERE id = %s", (config_id,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def create_config(
    *,
    provider: str,
    label: str,
    api_key_encrypted: str,
    model: Optional[str],
    base_url: Optional[str],
    activate: bool,
) -> dict:
    """Insert a new row. When `activate` is True, deactivates every
    other row and activates this one in the same transaction."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_provider_configs
                    (provider, label, api_key_encrypted, model, base_url, is_active)
                VALUES (%s, %s, %s, %s, %s, FALSE)
                RETURNING id
                """,
                (provider, label, api_key_encrypted, model, base_url),
            )
            new_id = cur.fetchone()[0]
            if activate:
                cur.execute("UPDATE ai_provider_configs SET is_active = FALSE WHERE id != %s", (new_id,))
                cur.execute(
                    "UPDATE ai_provider_configs SET is_active = TRUE, updated_at = now() WHERE id = %s",
                    (new_id,),
                )
        conn.commit()
    return get_config(new_id)  # type: ignore[return-value]


def update_config(
    config_id: int,
    *,
    label: Optional[str] = None,
    api_key_encrypted: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    model_set: bool = False,
    base_url_set: bool = False,
) -> Optional[dict]:
    """Partial update — only fields explicitly passed are touched.
    `model_set`/`base_url_set` distinguish "clear this field to NULL"
    from "leave it alone", since both start out as `None` otherwise."""
    sets: list[str] = []
    params: list = []
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if api_key_encrypted is not None:
        sets.append("api_key_encrypted = %s")
        params.append(api_key_encrypted)
    if model_set:
        sets.append("model = %s")
        params.append(model)
    if base_url_set:
        sets.append("base_url = %s")
        params.append(base_url)
    if not sets:
        return get_config(config_id)
    sets.append("updated_at = now()")
    params.append(config_id)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE ai_provider_configs SET {', '.join(sets)} WHERE id = %s",
                params,
            )
        conn.commit()
    return get_config(config_id)


def activate_config(config_id: int) -> Optional[dict]:
    """Deactivate every other row and activate this one, in a single
    transaction so two concurrent activations can't both appear to
    succeed."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_provider_configs SET is_active = FALSE WHERE id != %s", (config_id,))
            cur.execute(
                "UPDATE ai_provider_configs SET is_active = TRUE, updated_at = now() WHERE id = %s",
                (config_id,),
            )
            updated = cur.rowcount
        conn.commit()
    return get_config(config_id) if updated else None


def delete_config(config_id: int) -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_provider_configs WHERE id = %s", (config_id,))
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0
