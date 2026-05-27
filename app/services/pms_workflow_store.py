"""PMS Workflow store — Postgres-backed CRUD + state machine for the
PMS revision + signature system. Mirrors the `vsw_*` tables in the
Valvesheet backend so the lifecycle stays consistent between the two
products.

Tables (see db/init/01_pms_workflow.sql):
  • pms_workflows   — one row per (project, piping_class)
  • pms_revisions   — A0 / R0 / A1 / C0 / C1 / D0 / 00 / Z1 / P1 / XX
  • pms_signatures  — Prepared / Checked / Reviewed / Approved per revision
  • pms_snapshots   — frozen PMS payload (JSONB) per revision
  • pms_audit       — append-only action log
  • pms_changes     — change-identifiers when looping in the same code

Public API — see end of module.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from psycopg import Connection, OperationalError
from psycopg.rows import dict_row
import psycopg

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# State machine (1:1 with vsw_routes.py — keeps PMS aligned with valvesheet)
# ============================================================================
class State(str, Enum):
    INITIAL = "INITIAL"
    A0 = "A0"
    R0 = "R0"
    A1 = "A1"
    C0 = "C0"
    C1 = "C1"
    D0 = "D0"
    ZZ = "00"     # AFC (post-contract)
    Z1 = "Z1"     # As-built
    P1 = "P1"     # Info-only side-branch
    XX = "XX"     # Void terminal


ALLOWED: dict[State, set[State]] = {
    State.INITIAL: {State.A0},
    State.A0: {State.R0, State.A1, State.XX},
    State.R0: {State.A1, State.XX},
    State.A1: {State.A1, State.C0, State.XX},
    State.C0: {State.C1, State.XX},
    State.C1: {State.D0, State.XX},
    State.D0: {State.ZZ, State.D0, State.XX},
    State.ZZ: {State.Z1, State.ZZ, State.XX},
    State.Z1: {State.XX},
    State.P1: {State.XX},
    State.XX: set(),
}


PRE_CONTRACT = {State.A0, State.R0, State.A1, State.C0}
POST_CONTRACT = {State.C1, State.D0, State.ZZ, State.Z1}

ROLE_SIGNATURES: dict[str, set[str]] = {
    "MAKER":    {"PREPARED"},
    "CHECKER":  {"CHECKED", "REVIEWED"},
    "APPROVER": {"APPROVED"},
}

SIGNATURE_ORDER: list[str] = ["PREPARED", "CHECKED", "REVIEWED", "APPROVED"]


def derive_phase(state: State, fallback: str = "PRE_CONTRACT") -> str:
    if state in PRE_CONTRACT: return "PRE_CONTRACT"
    if state in POST_CONTRACT: return "POST_CONTRACT"
    return fallback


def signatures_required(code: State, is_rfq: bool) -> set[str]:
    """Same rule the VSW system uses."""
    base = {"PREPARED", "CHECKED", "REVIEWED"}
    if code in {State.A0, State.R0} or (code == State.A1 and not is_rfq):
        return base
    if code == State.INITIAL:
        return set()
    return base | {"APPROVED"}


def signatures_required_ordered(code: State, is_rfq: bool) -> list[str]:
    req = signatures_required(code, is_rfq)
    return [s for s in SIGNATURE_ORDER if s in req]


def make_label(code: State, counter: int) -> str:
    if code == State.A0: return "A0"
    if code == State.R0: return f"R{counter}"
    if code == State.A1: return f"A{counter + 1}"
    if code == State.C0: return "C0" if counter == 0 else f"C{counter + 1}"
    if code == State.C1: return f"C{counter + 1}"
    if code == State.D0: return f"D{counter}"
    if code == State.ZZ: return f"{counter:02d}"
    if code == State.Z1: return f"Z{counter + 1}"
    if code == State.P1: return f"P{counter + 1}"
    if code == State.XX: return "XX"
    return code.value


def can_user_sign(role: str, sig_type: str) -> bool:
    return sig_type in ROLE_SIGNATURES.get((role or "").upper(), set())


# ============================================================================
# Connection management
# ============================================================================
def _conn_url() -> str:
    # Read from settings (pydantic-settings picks up .env automatically)
    # so that DATABASE_URL=… in .env works without needing it exported to
    # the shell environment. Fall back to os.getenv for environments that
    # inject the var at the process level (e.g. Render, Docker).
    url = (settings.database_url or os.getenv("DATABASE_URL", "")).strip()
    if not url:
        raise StoreError("DATABASE_URL is not configured")
    # psycopg 3 accepts postgresql:// only
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class StoreError(RuntimeError):
    """Raised when the DB is unreachable or a query fails."""


def _connect() -> Connection:
    try:
        return psycopg.connect(_conn_url(), row_factory=dict_row, autocommit=True)
    except OperationalError as exc:
        raise StoreError(f"Postgres unreachable: {exc}") from exc


def init() -> None:
    """Create PMS workflow tables if they don't exist. Idempotent — safe
    to call on every startup. Called from FastAPI `on_event("startup")`."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_workflows (
                    id                    VARCHAR(36) PRIMARY KEY,
                    project_id            VARCHAR(50)  NOT NULL,
                    piping_class          VARCHAR(64)  NOT NULL,
                    document_title        VARCHAR(512) NOT NULL,
                    current_phase         VARCHAR(32)  NOT NULL DEFAULT 'PRE_CONTRACT',
                    current_state         VARCHAR(8)   NOT NULL DEFAULT 'INITIAL',
                    current_counter       INTEGER      NOT NULL DEFAULT 0,
                    current_revision_id   VARCHAR(36),
                    is_locked             BOOLEAN      NOT NULL DEFAULT FALSE,
                    created_by_user_id    VARCHAR(64),
                    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_pms_wf_project_class "
                "ON pms_workflows (project_id, piping_class)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_wf_project ON pms_workflows (project_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_wf_state ON pms_workflows (current_state)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_revisions (
                    id                          VARCHAR(36) PRIMARY KEY,
                    workflow_id                 VARCHAR(36) NOT NULL REFERENCES pms_workflows(id) ON DELETE CASCADE,
                    code                        VARCHAR(8)  NOT NULL,
                    counter                     INTEGER     NOT NULL DEFAULT 0,
                    revision_label              VARCHAR(16) NOT NULL,
                    is_rfq                      BOOLEAN     NOT NULL DEFAULT FALSE,
                    status                      VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
                    included_in_history         BOOLEAN     NOT NULL DEFAULT TRUE,
                    issued_by_user_id           VARCHAR(64),
                    issued_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    superseded_by_revision_id   VARCHAR(36) REFERENCES pms_revisions(id),
                    parent_revision_id          VARCHAR(36) REFERENCES pms_revisions(id)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_rev_workflow ON pms_revisions (workflow_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_rev_status ON pms_revisions (status)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_signatures (
                    id                       VARCHAR(36) PRIMARY KEY,
                    revision_id              VARCHAR(36) NOT NULL REFERENCES pms_revisions(id) ON DELETE CASCADE,
                    signature_type           VARCHAR(16) NOT NULL,
                    signed_by_user_id        VARCHAR(64) NOT NULL,
                    signer_role_at_signing   VARCHAR(32),
                    signer_name_snapshot     VARCHAR(255),
                    decision                 VARCHAR(16) NOT NULL DEFAULT 'APPROVED',
                    comment                  TEXT,
                    signed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    revoked                  BOOLEAN     NOT NULL DEFAULT FALSE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_sig_revision ON pms_signatures (revision_id)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_pms_sig_active "
                "ON pms_signatures (revision_id, signature_type) WHERE revoked = FALSE"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_snapshots (
                    id                    VARCHAR(36) PRIMARY KEY,
                    revision_id           VARCHAR(36) NOT NULL UNIQUE REFERENCES pms_revisions(id) ON DELETE CASCADE,
                    payload               JSONB       NOT NULL,
                    cached_excel_bytes    BYTEA,
                    created_by_user_id    VARCHAR(64),
                    updated_by_user_id    VARCHAR(64),
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_snap_revision ON pms_snapshots (revision_id)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_audit (
                    id                    VARCHAR(36) PRIMARY KEY,
                    workflow_id           VARCHAR(36) NOT NULL REFERENCES pms_workflows(id) ON DELETE CASCADE,
                    revision_id           VARCHAR(36) REFERENCES pms_revisions(id) ON DELETE SET NULL,
                    action                VARCHAR(64) NOT NULL,
                    from_state            VARCHAR(8),
                    to_state              VARCHAR(8),
                    actor_user_id         VARCHAR(64),
                    actor_name_snapshot   VARCHAR(255),
                    performed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    extra_metadata        JSONB
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_audit_workflow ON pms_audit (workflow_id)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pms_changes (
                    id                       VARCHAR(36) PRIMARY KEY,
                    revision_id              VARCHAR(36) NOT NULL REFERENCES pms_revisions(id) ON DELETE CASCADE,
                    from_revision_id         VARCHAR(36) REFERENCES pms_revisions(id) ON DELETE SET NULL,
                    identifier_code          VARCHAR(32),
                    description              TEXT        NOT NULL,
                    created_by_user_id       VARCHAR(64),
                    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_pms_changes_revision ON pms_changes (revision_id)"
            )

        logger.info("PMS workflow store: all tables ready.")
    except Exception as exc:
        logger.warning("PMS workflow store init failed: %s", exc)


# ============================================================================
# Helpers
# ============================================================================
def _now() -> datetime:
    return datetime.utcnow()


def _full_name(user: dict) -> str:
    return user.get("full_name") or user.get("email") or user.get("user_id") or ""


# ============================================================================
# Workflow CRUD
# ============================================================================
def create_workflow(
    *,
    project_id: str,
    piping_class: str,
    document_title: str,
    starting_state: str,
    snapshot_payload: dict,
    user: dict,
) -> dict:
    """Create a new workflow at the given state with an A0 revision
    holding the initial snapshot payload."""
    start = State(starting_state or "A0")
    if start not in ALLOWED[State.INITIAL]:
        raise StoreError(f"Cannot start at {start.value} — only A0 is supported")

    wf_id = uuid.uuid4().hex
    rev_id = uuid.uuid4().hex
    snap_id = uuid.uuid4().hex
    label = make_label(start, 0)
    phase = derive_phase(start)
    now = _now()

    with _connect() as conn, conn.cursor() as cur:
        # Uniqueness check
        cur.execute(
            "SELECT id FROM pms_workflows WHERE project_id = %s AND piping_class = %s",
            (project_id, piping_class),
        )
        if cur.fetchone():
            raise StoreError(
                f"A PMS workflow already exists for project {project_id} + class {piping_class}"
            )

        cur.execute(
            """INSERT INTO pms_workflows
            (id, project_id, piping_class, document_title, current_phase,
             current_state, current_counter, current_revision_id, is_locked,
             created_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)""",
            (wf_id, project_id, piping_class, document_title, phase,
             start.value, 0, rev_id, user["user_id"], now, now),
        )
        cur.execute(
            """INSERT INTO pms_revisions
            (id, workflow_id, code, counter, revision_label, is_rfq,
             status, included_in_history, issued_by_user_id, issued_at)
            VALUES (%s, %s, %s, %s, %s, FALSE, %s, TRUE, %s, %s)""",
            (rev_id, wf_id, start.value, 0, label, "DRAFT", user["user_id"], now),
        )
        cur.execute(
            """INSERT INTO pms_snapshots
            (id, revision_id, payload, created_by_user_id, updated_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (snap_id, rev_id, json.dumps(snapshot_payload), user["user_id"], user["user_id"], now, now),
        )
        _audit(cur, wf_id, rev_id, "CREATE_WORKFLOW", None, start.value, user)

    return {"workflow_id": wf_id, "revision_id": rev_id, "label": label}


def list_workflows(*, project_id: Optional[str] = None, piping_class: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM pms_workflows WHERE 1=1"
    args: list = []
    if project_id:
        sql += " AND project_id = %s"
        args.append(project_id)
    if piping_class:
        sql += " AND piping_class = %s"
        args.append(piping_class)
    sql += " ORDER BY created_at DESC"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def get_workflow(workflow_id: str) -> dict:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pms_workflows WHERE id = %s", (workflow_id,))
        wf = cur.fetchone()
        if not wf:
            raise StoreError("Workflow not found")
        cur.execute(
            "SELECT * FROM pms_revisions WHERE workflow_id = %s ORDER BY issued_at ASC",
            (workflow_id,),
        )
        revs = cur.fetchall()
        # Hydrate each revision with its signatures + changes
        for r in revs:
            cur.execute(
                "SELECT * FROM pms_signatures WHERE revision_id = %s ORDER BY signed_at ASC",
                (r["id"],),
            )
            r["signatures"] = cur.fetchall()
            cur.execute(
                "SELECT * FROM pms_changes WHERE revision_id = %s ORDER BY created_at ASC",
                (r["id"],),
            )
            r["changes"] = cur.fetchall()
        wf["revisions"] = revs
        return wf


# ============================================================================
# Snapshot CRUD
# ============================================================================
EDITABLE_KEYS: set[str] = {"service", "design_pressure_barg", "design_temp_c"}


def get_snapshot(revision_id: str) -> dict:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pms_snapshots WHERE revision_id = %s", (revision_id,))
        snap = cur.fetchone()
        if not snap:
            raise StoreError("No snapshot for this revision")
        return snap


def get_revision(revision_id: str) -> Optional[dict]:
    """Return the pms_revisions row for this id, or None."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pms_revisions WHERE id = %s", (revision_id,))
        return cur.fetchone()


def get_signatures(revision_id: str) -> list[dict]:
    """Return all non-revoked signatures for a revision, ordered by
    PREPARED → CHECKED → REVIEWED → APPROVED."""
    _ORDER = ["PREPARED", "CHECKED", "REVIEWED", "APPROVED"]
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pms_signatures WHERE revision_id = %s ORDER BY signed_at ASC",
            (revision_id,),
        )
        rows = cur.fetchall() or []
    # Keep only the latest non-revoked entry per slot
    sig_map: dict[str, dict] = {}
    for row in rows:
        if not row.get("revoked"):
            sig_map[row["signature_type"]] = row
    return [sig_map[s] for s in _ORDER if s in sig_map]


def get_piping_class_for_revision(revision_id: str) -> Optional[str]:
    """Return the piping_class of the workflow that owns this revision.
    Used by the download route to look up saved_pms when the snapshot
    payload pre-dates the top-level rating/material/ca stamping."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT w.piping_class
               FROM pms_revisions r
               JOIN pms_workflows w ON w.id = r.workflow_id
               WHERE r.id = %s""",
            (revision_id,),
        )
        row = cur.fetchone()
        return row["piping_class"] if row else None


def upsert_snapshot(revision_id: str, payload: dict, user: dict) -> None:
    """Update the three editable fields (service / design_pressure_barg /
    design_temp_c) on the snapshot. The full payload is merged so the
    frozen non-editable fields are preserved verbatim."""
    with _connect() as conn, conn.cursor() as cur:
        # Lock the revision and snapshot in one go
        cur.execute("SELECT * FROM pms_revisions WHERE id = %s", (revision_id,))
        rev = cur.fetchone()
        if not rev:
            raise StoreError("Revision not found")
        if rev["status"] in {"SIGNED", "VOIDED", "SUPERSEDED"}:
            raise StoreError(f"Revision is {rev['status']} — snapshot immutable")

        # Same "Maker has already signed PREPARED → locked unless REJECTED" rule as VSW.
        if rev["status"] != "REJECTED":
            cur.execute(
                "SELECT id FROM pms_signatures "
                "WHERE revision_id = %s AND signature_type = 'PREPARED' AND revoked = FALSE",
                (revision_id,),
            )
            if cur.fetchone():
                raise StoreError(
                    "Snapshot locked — Maker has already signed PREPARED on this revision. "
                    "Wait for review or have a reviewer reject."
                )

        cur.execute("SELECT * FROM pms_snapshots WHERE revision_id = %s", (revision_id,))
        existing = cur.fetchone()
        now = _now()
        if not existing:
            cur.execute(
                """INSERT INTO pms_snapshots
                (id, revision_id, payload, created_by_user_id, updated_by_user_id,
                 created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (uuid.uuid4().hex, revision_id, json.dumps(payload),
                 user["user_id"], user["user_id"], now, now),
            )
            return

        # Merge: start from the stored payload, apply only EDITABLE_KEYS
        # from the incoming request. All other keys are ignored — this
        # prevents float-precision round-trip differences (e.g. 17.382 vs
        # 17.382000000000001 in JSON) from incorrectly blocking saves.
        prev = existing["payload"] or {}
        merged = dict(prev)
        for k in EDITABLE_KEYS:
            if k in payload:
                merged[k] = payload[k]

        cur.execute(
            """UPDATE pms_snapshots
            SET payload = %s, updated_by_user_id = %s, updated_at = %s,
                cached_excel_bytes = NULL
            WHERE revision_id = %s""",
            (json.dumps(merged), user["user_id"], now, revision_id),
        )
        _audit(
            cur,
            rev["workflow_id"], revision_id, "ATTACH_SNAPSHOT",
            None, None, user,
            extra={"editable_changed": [
                k for k in EDITABLE_KEYS if payload.get(k) != prev.get(k)
            ]},
        )


# ============================================================================
# Transition (issue next revision)
# ============================================================================
def transition(
    workflow_id: str,
    *,
    target_state: str,
    is_rfq: bool,
    change_identifiers: list[dict],
    user: dict,
) -> dict:
    target = State(target_state)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pms_workflows WHERE id = %s", (workflow_id,))
        wf = cur.fetchone()
        if not wf:
            raise StoreError("Workflow not found")
        if wf["is_locked"]:
            raise StoreError("Workflow is locked")

        current = State(wf["current_state"])
        if target not in ALLOWED[current]:
            raise StoreError(f"Cannot transition from {current.value} to {target.value}")

        # Sign-off gate — all required signatures on the current revision
        # must be approved before moving forward.
        if wf["current_revision_id"]:
            cur.execute(
                "SELECT * FROM pms_revisions WHERE id = %s", (wf["current_revision_id"],),
            )
            prior_rev = cur.fetchone()
            required = signatures_required(State(prior_rev["code"]), prior_rev["is_rfq"])
            cur.execute(
                """SELECT signature_type FROM pms_signatures
                WHERE revision_id = %s AND revoked = FALSE AND decision = 'APPROVED'""",
                (wf["current_revision_id"],),
            )
            present = {row["signature_type"] for row in cur.fetchall()}
            missing = required - present
            if missing and target != State.XX:
                raise StoreError(f"Missing signatures on current revision: {sorted(missing)}")

        # Counter logic
        looping = target == current
        if looping:
            new_counter = wf["current_counter"] + 1
        else:
            new_counter = 0
        new_label = make_label(target, new_counter)
        phase = derive_phase(target, wf["current_phase"])
        now = _now()
        new_rev_id = uuid.uuid4().hex

        # Carry the prior snapshot forward into the new revision
        prior_snapshot_payload = {}
        if wf["current_revision_id"]:
            cur.execute(
                "SELECT payload FROM pms_snapshots WHERE revision_id = %s",
                (wf["current_revision_id"],),
            )
            prior = cur.fetchone()
            if prior:
                prior_snapshot_payload = prior["payload"] or {}

        cur.execute(
            """INSERT INTO pms_revisions
            (id, workflow_id, code, counter, revision_label, is_rfq, status,
             included_in_history, issued_by_user_id, issued_at, parent_revision_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)""",
            (new_rev_id, workflow_id, target.value, new_counter, new_label,
             bool(is_rfq), "PENDING_SIGNATURES", user["user_id"], now,
             wf["current_revision_id"]),
        )
        cur.execute(
            """INSERT INTO pms_snapshots
            (id, revision_id, payload, created_by_user_id, updated_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (uuid.uuid4().hex, new_rev_id, json.dumps(prior_snapshot_payload),
             user["user_id"], user["user_id"], now, now),
        )
        # Change identifiers
        for ci in (change_identifiers or []):
            cur.execute(
                """INSERT INTO pms_changes
                (id, revision_id, from_revision_id, identifier_code, description,
                 created_by_user_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (uuid.uuid4().hex, new_rev_id, wf["current_revision_id"],
                 ci.get("identifier_code"), ci.get("description") or "",
                 user["user_id"], now),
            )

        # Supersede prior
        if wf["current_revision_id"]:
            cur.execute(
                "UPDATE pms_revisions SET superseded_by_revision_id = %s WHERE id = %s",
                (new_rev_id, wf["current_revision_id"]),
            )
            cur.execute(
                """UPDATE pms_revisions SET status = 'SUPERSEDED'
                WHERE id = %s AND status NOT IN ('SIGNED', 'VOIDED')""",
                (wf["current_revision_id"],),
            )

        # Update workflow head
        is_locked = target == State.XX
        cur.execute(
            """UPDATE pms_workflows SET current_state = %s, current_counter = %s,
            current_phase = %s, current_revision_id = %s, is_locked = %s, updated_at = %s
            WHERE id = %s""",
            (target.value, new_counter, phase, new_rev_id, is_locked, now, workflow_id),
        )
        _audit(cur, workflow_id, new_rev_id, "TRANSITION", current.value, target.value, user)

    return {"revision_id": new_rev_id, "label": new_label}


def void_workflow(workflow_id: str, user: dict) -> dict:
    return transition(
        workflow_id,
        target_state="XX",
        is_rfq=False,
        change_identifiers=[],
        user=user,
    )


# ============================================================================
# Sign / Reject
# ============================================================================
def sign(
    revision_id: str,
    *,
    signature_type: str,
    decision: str,
    comment: Optional[str],
    user: dict,
) -> dict:
    sig_type = (signature_type or "").upper()
    decision = (decision or "APPROVED").upper()
    if sig_type not in {"PREPARED", "CHECKED", "REVIEWED", "APPROVED"}:
        raise StoreError(f"Unknown signature_type: {sig_type}")
    if decision not in {"APPROVED", "REJECTED"}:
        raise StoreError("decision must be APPROVED or REJECTED")
    if decision == "REJECTED" and not (comment and comment.strip()):
        raise StoreError("Comment is required when rejecting")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pms_revisions WHERE id = %s", (revision_id,))
        rev = cur.fetchone()
        if not rev:
            raise StoreError("Revision not found")
        cur.execute("SELECT * FROM pms_workflows WHERE id = %s", (rev["workflow_id"],))
        wf = cur.fetchone()
        if wf["is_locked"]:
            raise StoreError("Workflow is locked")

        # On REJECTED, only Maker re-prep (PREPARED) is permitted
        if rev["status"] == "REJECTED" and sig_type != "PREPARED":
            raise StoreError(
                "Revision is REJECTED. Only the Maker can re-sign PREPARED to restart the cycle."
            )

        required = signatures_required(State(rev["code"]), rev["is_rfq"])
        if sig_type not in required:
            raise StoreError(f"{sig_type} not required for code {rev['code']}")

        role = (user.get("role_code") or "").upper()
        if not can_user_sign(role, sig_type):
            raise StoreError(f"Role {role or '(none)'} cannot apply {sig_type}")

        # Sequential signing
        ordered = signatures_required_ordered(State(rev["code"]), rev["is_rfq"])
        cur.execute(
            """SELECT signature_type FROM pms_signatures
            WHERE revision_id = %s AND revoked = FALSE AND decision = 'APPROVED'""",
            (revision_id,),
        )
        approved = {row["signature_type"] for row in cur.fetchall()}
        next_expected = next((s for s in ordered if s not in approved), None)
        if sig_type != next_expected:
            raise StoreError(
                f"Out-of-order signature. Next expected slot is "
                f"{next_expected or 'none'}; you cannot sign {sig_type} yet."
            )

        # Separation of duties
        cur.execute(
            """SELECT signature_type FROM pms_signatures
            WHERE revision_id = %s AND signed_by_user_id = %s AND revoked = FALSE""",
            (revision_id, user["user_id"]),
        )
        same_user = cur.fetchone()
        if same_user:
            raise StoreError(
                f"You already signed as {same_user['signature_type']} on this revision."
            )

        # Insert the new signature
        sig_id = uuid.uuid4().hex
        now = _now()
        cur.execute(
            """INSERT INTO pms_signatures
            (id, revision_id, signature_type, signed_by_user_id, signer_role_at_signing,
             signer_name_snapshot, decision, comment, signed_at, revoked)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)""",
            (sig_id, revision_id, sig_type, user["user_id"], role, _full_name(user),
             decision, (comment.strip() if comment and comment.strip() else None), now),
        )

        # REJECTION CASCADE
        if decision == "REJECTED":
            cur.execute(
                """UPDATE pms_signatures SET revoked = TRUE
                WHERE revision_id = %s AND id <> %s AND revoked = FALSE""",
                (revision_id, sig_id),
            )
            new_status = "REJECTED"
        else:
            # Maker re-prep on a REJECTED revision: wipe lingering rejection rows
            # and flip back to PENDING_SIGNATURES.
            if rev["status"] == "REJECTED":
                cur.execute(
                    """UPDATE pms_signatures SET revoked = TRUE
                    WHERE revision_id = %s AND id <> %s AND revoked = FALSE""",
                    (revision_id, sig_id),
                )
                base_status = "PENDING_SIGNATURES"
            else:
                base_status = rev["status"]
            cur.execute(
                """SELECT signature_type FROM pms_signatures
                WHERE revision_id = %s AND revoked = FALSE AND decision = 'APPROVED'""",
                (revision_id,),
            )
            present = {row["signature_type"] for row in cur.fetchall()}
            new_status = "SIGNED" if required.issubset(present) else base_status

        cur.execute(
            "UPDATE pms_revisions SET status = %s WHERE id = %s",
            (new_status, revision_id),
        )
        _audit(
            cur,
            rev["workflow_id"], revision_id,
            "SIGN_REJECTED" if decision == "REJECTED" else "SIGN",
            None, None, user,
            extra={"signature_type": sig_type, "decision": decision,
                   "revision_label": rev["revision_label"],
                   "cascaded_revoke": decision == "REJECTED"},
        )
    return {"signature_id": sig_id, "new_revision_status": new_status}


# ============================================================================
# Audit
# ============================================================================
def _audit(cur, workflow_id: str, revision_id: Optional[str], action: str,
           from_state: Optional[str], to_state: Optional[str], user: dict,
           extra: Optional[dict] = None) -> None:
    cur.execute(
        """INSERT INTO pms_audit
        (id, workflow_id, revision_id, action, from_state, to_state,
         actor_user_id, actor_name_snapshot, performed_at, extra_metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (uuid.uuid4().hex, workflow_id, revision_id, action,
         from_state, to_state, user["user_id"], _full_name(user),
         _now(), json.dumps(extra) if extra is not None else None),
    )


def audit(workflow_id: str) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM pms_audit WHERE workflow_id = %s ORDER BY performed_at ASC",
            (workflow_id,),
        )
        return cur.fetchall()


# ============================================================================
# State-machine info (for the frontend dialogs)
# ============================================================================
def state_machine_info() -> dict:
    return {
        "transitions": {s.value: sorted(t.value for t in ALLOWED[s]) for s in ALLOWED},
        "signatures_required": {
            c.value: {
                "non_rfq": signatures_required_ordered(c, False),
                "rfq": signatures_required_ordered(c, True),
            } for c in [State.A0, State.R0, State.A1, State.C0, State.C1,
                       State.D0, State.ZZ, State.Z1, State.P1, State.XX]
        },
        "role_signatures": {r: sorted(v) for r, v in ROLE_SIGNATURES.items()},
        "signature_order": SIGNATURE_ORDER,
        "phases": {
            "pre_contract": [s.value for s in PRE_CONTRACT],
            "post_contract": [s.value for s in POST_CONTRACT],
        },
        "editable_keys": sorted(EDITABLE_KEYS),
    }
