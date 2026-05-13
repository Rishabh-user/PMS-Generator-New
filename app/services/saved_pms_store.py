"""Postgres-backed persistence for user-saved PMS entries.

Each entry is a class the user picked from the AI Agent's match cards
(or the standalone PMS Generator page) and explicitly chose to keep
around for later. Distinct from `pms_agent_sessions` (raw chat
transcripts) — this is the curated short-list.

Schema:
    saved_pms (
        id                    SERIAL PRIMARY KEY,
        user_id               TEXT NOT NULL,
        piping_class          TEXT NOT NULL,
        rating                TEXT NOT NULL,
        material              TEXT NOT NULL,
        corrosion_allowance   TEXT NOT NULL,
        service               TEXT NOT NULL DEFAULT '',
        design_pressure_barg  DOUBLE PRECISION,
        design_temp_c         DOUBLE PRECISION,
        mdmt_c                DOUBLE PRECISION,
        joint_type            TEXT,
        payload               JSONB NOT NULL DEFAULT '{}'::jsonb,   -- full resolve-class output
        note                  TEXT NOT NULL DEFAULT '',
        saved_at              DOUBLE PRECISION NOT NULL,
        updated_at            DOUBLE PRECISION NOT NULL,
        UNIQUE (user_id, piping_class, rating, material, corrosion_allowance)
    )

Identity = (user, piping class, rating, material, corrosion allowance).
Service is treated as metadata, not part of identity: saving the same
class with a different service updates the existing row's service
field rather than creating a duplicate row. Engineering rationale:
"PMS A1 at 150# CS 3mm" is one specification — using it for Cooling
Media or for Glycol doesn't change the spec.

`payload` carries the full output of class_resolver.resolve() — class
code + pressure-temperature curve + stress / Y / fitting / flange /
branch-chart code factors + WT table snapshot + materials-tab
snapshot + adequacy + derived design conditions. Everything the
frontend needs to re-render the full PMS report at recall time
without re-resolving.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.services import session_store
from app.services.session_store import SessionStoreUnavailableError

logger = logging.getLogger(__name__)


# The unique-constraint name we want enforced after the schema settles.
# Postgres auto-generates names for table-level UNIQUE constraints based
# on the columns, so installs predating the dedup-key change will have a
# different (much longer) auto-name. The migration code below drops any
# old auto-named UNIQUE on saved_pms whose columns include `service`, then
# adds this one. The explicit name makes ON CONFLICT use trivial.
_DEDUP_CONSTRAINT_NAME = "saved_pms_dedup_key"


def _migrate_dedup_key(cur) -> None:
    """One-time migration: drop the old (…, service) UNIQUE constraint,
    dedupe any rows that collide under the new key (keeping the most
    recently updated one), and add the new UNIQUE without `service`.

    Idempotent — running again is a no-op once the new constraint
    exists."""
    # Already migrated?
    cur.execute(
        """
        SELECT 1 FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE nsp.nspname = 'public' AND cls.relname = 'saved_pms'
          AND con.conname = %s
        """,
        (_DEDUP_CONSTRAINT_NAME,),
    )
    if cur.fetchone():
        return

    # Find existing UNIQUE constraints on the table that include
    # `service` AND `piping_class` — the legacy auto-generated key.
    cur.execute(
        """
        SELECT con.conname,
               array_agg(att.attname ORDER BY array_position(con.conkey, att.attnum)) AS cols
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        JOIN pg_attribute att ON att.attrelid = con.conrelid
                              AND att.attnum = ANY (con.conkey)
        WHERE nsp.nspname = 'public' AND cls.relname = 'saved_pms'
          AND con.contype = 'u'
        GROUP BY con.conname
        """
    )
    legacy_names: list[str] = []
    for name, cols in cur.fetchall():
        if "service" in cols and "piping_class" in cols:
            legacy_names.append(name)

    # Dedupe rows that would collide under the new key. Keep the row
    # with the latest updated_at (ties broken by largest id).
    cur.execute(
        """
        DELETE FROM saved_pms a USING saved_pms b
        WHERE a.user_id = b.user_id
          AND a.piping_class = b.piping_class
          AND a.rating = b.rating
          AND a.material = b.material
          AND a.corrosion_allowance = b.corrosion_allowance
          AND a.id <> b.id
          AND (
              a.updated_at < b.updated_at
              OR (a.updated_at = b.updated_at AND a.id < b.id)
          )
        """
    )

    for name in legacy_names:
        cur.execute(f'ALTER TABLE saved_pms DROP CONSTRAINT "{name}"')

    cur.execute(
        f'ALTER TABLE saved_pms ADD CONSTRAINT "{_DEDUP_CONSTRAINT_NAME}" '
        "UNIQUE (user_id, piping_class, rating, material, corrosion_allowance)"
    )
    logger.info(
        "saved_pms dedup key migrated — dropped %d legacy constraint(s), "
        "added %s on (user_id, piping_class, rating, material, "
        "corrosion_allowance)",
        len(legacy_names), _DEDUP_CONSTRAINT_NAME,
    )


def init() -> None:
    """Create the saved_pms table on app startup. Idempotent.

    Also runs an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for installs
    that were created before the payload / mdmt / joint_type columns
    were added, plus _migrate_dedup_key() to drop the old (…, service)
    UNIQUE and replace it with one that ignores service. Both are
    invisible to operators."""
    if not session_store.is_enabled():
        logger.warning(
            "DATABASE_URL not configured — saved_pms table will not be created."
        )
        return
    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS saved_pms (
                        id                    SERIAL PRIMARY KEY,
                        user_id               TEXT NOT NULL,
                        piping_class          TEXT NOT NULL,
                        rating                TEXT NOT NULL,
                        material              TEXT NOT NULL,
                        corrosion_allowance   TEXT NOT NULL,
                        service               TEXT NOT NULL DEFAULT '',
                        design_pressure_barg  DOUBLE PRECISION,
                        design_temp_c         DOUBLE PRECISION,
                        mdmt_c                DOUBLE PRECISION,
                        joint_type            TEXT,
                        payload               JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        note                  TEXT NOT NULL DEFAULT '',
                        saved_at              DOUBLE PRECISION NOT NULL,
                        updated_at            DOUBLE PRECISION NOT NULL,
                        CONSTRAINT "{_DEDUP_CONSTRAINT_NAME}"
                            UNIQUE (user_id, piping_class, rating,
                                    material, corrosion_allowance)
                    )
                    """
                )
                # Idempotent column adds for installs predating these fields.
                cur.execute(
                    "ALTER TABLE saved_pms "
                    "ADD COLUMN IF NOT EXISTS mdmt_c DOUBLE PRECISION"
                )
                cur.execute(
                    "ALTER TABLE saved_pms "
                    "ADD COLUMN IF NOT EXISTS joint_type TEXT"
                )
                cur.execute(
                    "ALTER TABLE saved_pms "
                    "ADD COLUMN IF NOT EXISTS payload JSONB "
                    "NOT NULL DEFAULT '{}'::jsonb"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_saved_pms_user_saved_at "
                    "ON saved_pms(user_id, saved_at DESC)"
                )
                # Drop the legacy (…, service) UNIQUE if it's still
                # there (left over from an older install).
                _migrate_dedup_key(cur)
            conn.commit()
        logger.info("saved_pms table initialised.")
    except SessionStoreUnavailableError as e:
        logger.warning(
            "Could not initialise saved_pms table: %s. "
            "Save endpoints will return 503 until the DB is reachable.",
            e,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error initialising saved_pms: %s", e)


def find_existing(
    user_id: str,
    piping_class: str,
    rating: str,
    material: str,
    corrosion_allowance: str,
    service: str = "",  # kept in the signature for source compat; ignored
) -> Optional[dict]:
    """Return the existing row (id + timestamps + previously-stored
    service) for a natural-key match, or None when nothing's stored
    yet. Identity = (user, class, rating, material, CA). Service is
    metadata — re-saving with a different service refreshes the stored
    one, it doesn't create a new row.

    The `service` kwarg is preserved for API stability but is not used
    in the WHERE clause."""
    del service  # explicit reminder that we no longer match on this
    with session_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, saved_at, updated_at, design_pressure_barg,
                       design_temp_c, mdmt_c, joint_type, service
                FROM saved_pms
                WHERE user_id = %s AND piping_class = %s AND rating = %s
                  AND material = %s AND corrosion_allowance = %s
                """,
                (user_id, piping_class, rating, material, corrosion_allowance),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id":                   int(row[0]),
        "saved_at":             float(row[1]),
        "updated_at":           float(row[2]),
        "design_pressure_barg": row[3],
        "design_temp_c":        row[4],
        "mdmt_c":               row[5],
        "joint_type":           row[6],
        "service":              row[7],
    }


def upsert(
    user_id: str,
    piping_class: str,
    rating: str,
    material: str,
    corrosion_allowance: str,
    service: str = "",
    design_pressure_barg: Optional[float] = None,
    design_temp_c: Optional[float] = None,
    mdmt_c: Optional[float] = None,
    joint_type: Optional[str] = None,
    payload: Optional[dict] = None,
    note: str = "",
) -> dict:
    """Insert a saved PMS row, or refresh design conditions + payload
    + service + timestamp when the same (user, class, rating, material,
    CA) combo already exists. Service is metadata, not part of the
    dedup key — on conflict the existing row's service is overwritten
    with the latest pick. Returns {id, created, saved_at}."""
    now = time.time()
    payload_json = json.dumps(payload or {})
    with session_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM saved_pms
                WHERE user_id = %s AND piping_class = %s AND rating = %s
                  AND material = %s AND corrosion_allowance = %s
                """,
                (user_id, piping_class, rating, material, corrosion_allowance),
            )
            existing = cur.fetchone()
            created = existing is None

            cur.execute(
                f"""
                INSERT INTO saved_pms
                    (user_id, piping_class, rating, material, corrosion_allowance,
                     service, design_pressure_barg, design_temp_c, mdmt_c,
                     joint_type, payload, note, saved_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT "{_DEDUP_CONSTRAINT_NAME}"
                DO UPDATE SET
                    service              = EXCLUDED.service,
                    design_pressure_barg = EXCLUDED.design_pressure_barg,
                    design_temp_c        = EXCLUDED.design_temp_c,
                    mdmt_c               = EXCLUDED.mdmt_c,
                    joint_type           = EXCLUDED.joint_type,
                    payload              = EXCLUDED.payload,
                    note                 = EXCLUDED.note,
                    updated_at           = EXCLUDED.updated_at
                RETURNING id
                """,
                (
                    user_id, piping_class, rating, material, corrosion_allowance,
                    service, design_pressure_barg, design_temp_c, mdmt_c,
                    joint_type, payload_json, note, now, now,
                ),
            )
            row_id = cur.fetchone()[0]
        conn.commit()

    return {"id": int(row_id), "created": created, "saved_at": now}
