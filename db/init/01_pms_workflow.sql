-- =============================================================================
-- PMS Workflow — revision + 4-signature system for PMS datasheets.
--
-- Mirrors the `vsw_*` tables in the Valvesheet backend so the lifecycle
-- (A0 → R0 → A1 → C0 → C1 → D0 → 00 → Z1, plus P1 side-branch + XX void)
-- and the Prepared / Checked / Reviewed / Approved signature flow stay
-- identical between the two products.
--
-- Identity of a workflow: one row per (project_id, piping_class). User
-- IDs come from the VDS user-management backend — no FK because that's
-- a different database; we store the user_id as a plain string.
--
-- Idempotent. Bootstrapped via docker-entrypoint-initdb.d on first boot
-- of the Postgres container (see docker-compose.yml).
-- =============================================================================

-- ──────────────────────────── workflows ─────────────────────────────────────
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
);

-- Uniqueness: one workflow per project+class. The Create page checks
-- this before submitting and offers "Open existing" if a hit is found.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pms_wf_project_class
    ON pms_workflows (project_id, piping_class);

CREATE INDEX IF NOT EXISTS idx_pms_wf_project ON pms_workflows (project_id);
CREATE INDEX IF NOT EXISTS idx_pms_wf_class   ON pms_workflows (piping_class);
CREATE INDEX IF NOT EXISTS idx_pms_wf_state   ON pms_workflows (current_state);


-- ──────────────────────────── revisions ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS pms_revisions (
    id                          VARCHAR(36) PRIMARY KEY,
    workflow_id                 VARCHAR(36) NOT NULL REFERENCES pms_workflows(id) ON DELETE CASCADE,
    code                        VARCHAR(8)  NOT NULL,        -- A0/R0/A1/C0/C1/D0/00/Z1/P1/XX
    counter                     INTEGER     NOT NULL DEFAULT 0,
    revision_label              VARCHAR(16) NOT NULL,        -- A0, R1, A2, C1, 00, Z1...
    is_rfq                      BOOLEAN     NOT NULL DEFAULT FALSE,
    status                      VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
                                                              -- DRAFT, PENDING_SIGNATURES, SIGNED, REJECTED,
                                                              -- SUPERSEDED, VOIDED
    included_in_history         BOOLEAN     NOT NULL DEFAULT TRUE,
    issued_by_user_id           VARCHAR(64),
    issued_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by_revision_id   VARCHAR(36) REFERENCES pms_revisions(id),
    parent_revision_id          VARCHAR(36) REFERENCES pms_revisions(id)
);

CREATE INDEX IF NOT EXISTS idx_pms_rev_workflow ON pms_revisions (workflow_id);
CREATE INDEX IF NOT EXISTS idx_pms_rev_status   ON pms_revisions (status);


-- ──────────────────────────── signatures ────────────────────────────────────
-- Up to 4 per revision (Prepared / Checked / Reviewed / Approved). One
-- active row per slot — old rows are revoked (not deleted) on a
-- rejection cascade or Maker re-prep. The partial unique index lets
-- many revoked rows coexist with a single non-revoked one.
CREATE TABLE IF NOT EXISTS pms_signatures (
    id                       VARCHAR(36) PRIMARY KEY,
    revision_id              VARCHAR(36) NOT NULL REFERENCES pms_revisions(id) ON DELETE CASCADE,
    signature_type           VARCHAR(16) NOT NULL,           -- PREPARED|CHECKED|REVIEWED|APPROVED
    signed_by_user_id        VARCHAR(64) NOT NULL,
    signer_role_at_signing   VARCHAR(32),
    signer_name_snapshot     VARCHAR(255),                   -- frozen at sign time
    decision                 VARCHAR(16) NOT NULL DEFAULT 'APPROVED',  -- APPROVED|REJECTED
    comment                  TEXT,
    signed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked                  BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_pms_sig_revision ON pms_signatures (revision_id);
CREATE INDEX IF NOT EXISTS idx_pms_sig_user     ON pms_signatures (signed_by_user_id);

-- Partial unique index — only one ACTIVE signature per (revision, slot).
CREATE UNIQUE INDEX IF NOT EXISTS uq_pms_sig_active
    ON pms_signatures (revision_id, signature_type)
    WHERE revoked = FALSE;


-- ──────────────────────────── snapshots ─────────────────────────────────────
-- Frozen PMS payload per revision. This is what `excel_exporter` uses
-- to render the xlsx so the same revision always produces the same
-- output, regardless of later changes to `saved_pms` or the rule engine.
--
-- The Maker can only edit three fields on a revision's snapshot:
--   • service
--   • design_pressure_barg
--   • design_temp_c
-- Everything else (class code, P-T table, materials, branch chart,
-- wall thickness, etc.) is carried forward from the parent revision.
CREATE TABLE IF NOT EXISTS pms_snapshots (
    id                    VARCHAR(36) PRIMARY KEY,
    revision_id           VARCHAR(36) NOT NULL UNIQUE REFERENCES pms_revisions(id) ON DELETE CASCADE,
    payload               JSONB       NOT NULL,
    cached_excel_bytes    BYTEA,
    created_by_user_id    VARCHAR(64),
    updated_by_user_id    VARCHAR(64),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pms_snap_revision ON pms_snapshots (revision_id);


-- ──────────────────────────── audit ─────────────────────────────────────────
-- Append-only log of every state transition / sign / reject / void.
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
);

CREATE INDEX IF NOT EXISTS idx_pms_audit_workflow ON pms_audit (workflow_id);
CREATE INDEX IF NOT EXISTS idx_pms_audit_revision ON pms_audit (revision_id);


-- ──────────────────────────── changes (optional, for traceability) ──────────
-- One row per "what changed" the Maker tagged when looping back to the
-- same revision code (e.g. A1 → A2 re-issue). Same shape as vsw_changes.
CREATE TABLE IF NOT EXISTS pms_changes (
    id                       VARCHAR(36) PRIMARY KEY,
    revision_id              VARCHAR(36) NOT NULL REFERENCES pms_revisions(id) ON DELETE CASCADE,
    from_revision_id         VARCHAR(36) REFERENCES pms_revisions(id) ON DELETE SET NULL,
    identifier_code          VARCHAR(32),
    description              TEXT        NOT NULL,
    created_by_user_id       VARCHAR(64),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pms_changes_revision ON pms_changes (revision_id);
