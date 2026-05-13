"""Admin endpoints — read-only browser for the PMS Postgres database.

Surface:
    GET /api/admin/tables
        → [{ name, row_count, columns: [{ name, type, nullable }] }, ...]

    GET /api/admin/tables/{name}/rows?limit=50&offset=0
        → { name, columns, rows, total, limit, offset }

Notes:
  • All table identifiers go through `psycopg.sql.Identifier`, so the
    `{name}` path parameter is properly quoted — no SQL injection.
  • Only tables in the `public` schema are exposed; system tables and
    other schemas are off-limits.
  • Row count uses an exact `COUNT(*)` for now. For very large tables
    that's slow; swap to `pg_class.reltuples` if any table grows past
    ~100k rows.
  • No auth on the backend — the SPA gates the admin page on access
    level Admin. If the backend is ever exposed publicly, add a shared
    secret header check here.
"""
from __future__ import annotations

import logging

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from psycopg import sql as psql

from app.services import session_store
from app.services.session_store import SessionStoreUnavailableError


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Database unavailable: {detail}")


def _verify_table_exists(cur, name: str) -> None:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (name,),
    )
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"Table '{name}' not found")


def _coerce_cell(value):
    """Make a row cell JSON-safe. Datetimes → ISO; bytes → hex preview;
    JSONB columns come back already parsed."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    # datetime / date / time
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # bytes
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return f"<{len(bytes(value))} bytes>"
    # Decimal, UUID, etc.
    return str(value)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/tables")
def list_tables() -> list[dict]:
    """List every table in the public schema with column metadata and row count."""
    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """
                )
                names = [r[0] for r in cur.fetchall()]

                result: list[dict] = []
                for t in names:
                    cur.execute(
                        """
                        SELECT column_name, data_type, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = %s
                        ORDER BY ordinal_position
                        """,
                        (t,),
                    )
                    columns = [
                        {"name": r[0], "type": r[1], "nullable": r[2] == "YES"}
                        for r in cur.fetchall()
                    ]
                    cur.execute(
                        psql.SQL("SELECT COUNT(*) FROM {}").format(psql.Identifier(t))
                    )
                    count = int(cur.fetchone()[0])
                    result.append({
                        "name":      t,
                        "row_count": count,
                        "columns":   columns,
                    })
                return result
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e)) from e


@router.get("/tables/{name}/rows")
def get_table_rows(
    name: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Fetch paginated rows from a public-schema table. Identifier is
    quoted, so SQL injection isn't possible via the path."""
    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                _verify_table_exists(cur, name)

                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (name,),
                )
                columns = [r[0] for r in cur.fetchall()]

                cur.execute(
                    psql.SQL("SELECT COUNT(*) FROM {}").format(psql.Identifier(name))
                )
                total = int(cur.fetchone()[0])

                # ORDER BY 1 keeps results stable across paginated calls.
                # If the first column isn't a sensible sort key the user
                # can still flip pages, just with an order tied to that
                # column's natural ordering.
                query = psql.SQL(
                    "SELECT * FROM {} ORDER BY 1 LIMIT %s OFFSET %s"
                ).format(psql.Identifier(name))
                cur.execute(query, (limit, offset))
                raw_rows = cur.fetchall()

                rows = [
                    {col: _coerce_cell(raw[i]) for i, col in enumerate(columns)}
                    for raw in raw_rows
                ]

                return {
                    "name":    name,
                    "columns": columns,
                    "rows":    rows,
                    "total":   total,
                    "limit":   limit,
                    "offset":  offset,
                }
    except HTTPException:
        raise
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e)) from e


# ---------------------------------------------------------------------------
# DELETE — remove a single row by primary key
# ---------------------------------------------------------------------------

def _primary_key_columns(cur, name: str) -> list[str]:
    """Return the ordered list of column names that make up `name`'s
    primary key. Composite PKs (e.g. pms_agent_sessions uses (id,
    user_id)) come back in the canonical column order."""
    cur.execute(
        """
        SELECT a.attname
        FROM   pg_index i
        JOIN   pg_attribute a ON a.attrelid = i.indrelid
                              AND a.attnum  = ANY (i.indkey)
        WHERE  i.indrelid = (
                   SELECT oid FROM pg_class
                   WHERE relname = %s
                     AND relnamespace = (
                         SELECT oid FROM pg_namespace WHERE nspname = 'public'
                     )
               )
        AND    i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (name,),
    )
    return [r[0] for r in cur.fetchall()]


@router.delete("/tables/{name}/rows")
def delete_row(
    name: str,
    body: dict[str, Any] = Body(..., description="Primary-key column → value map identifying the row"),
) -> dict:
    """Delete a single row from a public-schema table.

    The request body is `{ "<pk_col>": <value>, … }` — every PK column
    must be supplied. We refuse to delete:
      • tables without a primary key (can't uniquely target a row)
      • when the supplied body doesn't cover every PK column
      • when the body would match zero rows (returns 404)

    No SQL injection risk: PK column names are resolved from
    information_schema and the values are passed as parameters.
    """
    try:
        with session_store.connect() as conn:
            with conn.cursor() as cur:
                _verify_table_exists(cur, name)

                pk_cols = _primary_key_columns(cur, name)
                if not pk_cols:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Table '{name}' has no primary key; delete-by-key not supported.",
                    )

                missing = [c for c in pk_cols if c not in body]
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Missing primary key column(s) {missing} for table '{name}'. "
                            f"Required: {pk_cols}"
                        ),
                    )

                where_clause = psql.SQL(" AND ").join(
                    psql.SQL("{} = %s").format(psql.Identifier(c)) for c in pk_cols
                )
                query = psql.SQL("DELETE FROM {} WHERE {}").format(
                    psql.Identifier(name), where_clause,
                )
                params = tuple(body[c] for c in pk_cols)
                cur.execute(query, params)
                deleted = cur.rowcount
            conn.commit()

        if deleted == 0:
            raise HTTPException(status_code=404, detail="Row not found")
        return {"ok": True, "deleted": deleted}
    except HTTPException:
        raise
    except SessionStoreUnavailableError as e:
        raise _db_unavailable(str(e)) from e
