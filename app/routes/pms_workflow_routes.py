"""PMS Workflow routes — mirrors `vsw_routes.py` for the Valvesheet
backend. Verifies the same VDS JWT, manages PMS workflow + revisions +
signatures + frozen snapshots in this app's Postgres.

Endpoints (all prefixed `/api/pms-workflow`):

    GET    /state-machine                  — transitions / required sigs / roles
    GET    /pms-classes                    — autocomplete list of saved PMS classes
    GET    /workflows                      — list workflows (filter by project / class)
    POST   /workflows                      — create new workflow (find-or-generate snapshot)
    GET    /workflows/{id}                 — full detail (revisions + signatures + changes)
    POST   /workflows/{id}/transition      — issue next revision
    POST   /workflows/{id}/void            — terminal XX
    GET    /workflows/{id}/audit           — append-only action log

    POST   /revisions/{id}/sign            — sign / reject one slot
    GET    /revisions/{id}/snapshot        — frozen PMS payload
    POST   /revisions/{id}/snapshot        — edit only service / pressure / temp
    GET    /revisions/{id}/download        — xlsx of this revision's snapshot
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import (
    excel_exporter,
    pms_snapshot,
    pms_workflow_store as store,
    saved_pms_store,
    session_store,
)
from app.services.pms_workflow_store import StoreError
from app.services.vds_auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pms-workflow", tags=["PMS Workflow"])


# ============================================================================
# Pydantic request bodies
# ============================================================================
class WorkflowCreate(BaseModel):
    project_id: str
    # The piping class can be supplied directly OR derived from
    # rating + material + corrosion_allowance via the engine. The Create
    # form drives the latter — same way the existing PMS Generator page
    # picks options.
    piping_class: Optional[str] = None
    document_title: Optional[str] = None
    rating: Optional[str] = None
    material: Optional[str] = None
    corrosion_allowance: Optional[str] = None
    service: Optional[str] = None
    design_pressure_barg: Optional[float] = None
    design_temp_c: Optional[float] = None
    mdmt_c: Optional[float] = None
    joint_type: Optional[str] = None


class ChangeIn(BaseModel):
    identifier_code: Optional[str] = None
    description: str


class TransitionIn(BaseModel):
    target_state: str
    is_rfq: bool = False
    change_identifiers: list[ChangeIn] = []


class SignIn(BaseModel):
    signature_type: str
    decision: str = "APPROVED"
    comment: Optional[str] = None


class SnapshotIn(BaseModel):
    payload: dict
    # When True the incoming payload fully replaces the stored snapshot
    # (used after a live recompute in the Edit Snapshot dialog). When
    # False (default) only the three editable knobs are merged on top.
    full_replace: bool = False


# ============================================================================
# Helpers
# ============================================================================
def _store_to_http(exc: StoreError) -> HTTPException:
    msg = str(exc)
    code = 500
    if "not found" in msg.lower():
        code = 404
    elif "already exists" in msg.lower() or "uniqueness" in msg.lower():
        code = 409
    elif "locked" in msg.lower() or "immutable" in msg.lower() or "out-of-order" in msg.lower():
        code = 409
    elif "role" in msg.lower() or "permitted" in msg.lower() or "cannot apply" in msg.lower():
        code = 403
    elif "required" in msg.lower() or "decision must" in msg.lower():
        code = 422
    elif "not configured" in msg.lower() or "unreachable" in msg.lower() or "postgres" in msg.lower():
        code = 503
    return HTTPException(status_code=code, detail=msg)


def _lookup_saved_pms_payload(piping_class: str) -> Optional[dict]:
    """Find ANY saved_pms row for this piping_class (regardless of user)
    and return its payload. Used as the seed snapshot for a fresh
    workflow."""
    try:
        with session_store.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT payload, rating, material, corrosion_allowance, service,
                          design_pressure_barg, design_temp_c, mdmt_c, joint_type
                FROM saved_pms WHERE piping_class = %s
                ORDER BY updated_at DESC LIMIT 1""",
                (piping_class,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "payload": row[0] or {},
                "rating": row[1],
                "material": row[2],
                "corrosion_allowance": row[3],
                "service": row[4] or "",
                "design_pressure_barg": row[5],
                "design_temp_c": row[6],
                "mdmt_c": row[7],
                "joint_type": row[8],
            }
    except Exception as exc:
        logger.warning("saved_pms lookup failed for %s: %s", piping_class, exc)
        return None


def _generate_snapshot(req: WorkflowCreate) -> dict:
    """Call the existing pms_snapshot.build_pms_snapshot pipeline."""
    if not (req.rating and req.material and req.corrosion_allowance):
        raise HTTPException(
            status_code=422,
            detail=(
                f"No saved PMS exists for piping_class={req.piping_class}. "
                "Provide rating + material + corrosion_allowance "
                "so the engine can generate a fresh snapshot."
            ),
        )
    try:
        return pms_snapshot.build_pms_snapshot(
            rating=req.rating,
            material=req.material,
            corrosion_allowance=req.corrosion_allowance,
            service=req.service or "",
            design_pressure_barg=req.design_pressure_barg,
            design_temp_c=req.design_temp_c,
            mdmt_c=req.mdmt_c,
            joint_type=req.joint_type,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Snapshot generation failed: {exc}",
        )


def _seed_editable(snapshot: dict, req: WorkflowCreate, saved: Optional[dict]) -> dict:
    """Layer user-supplied editable fields (service / design_pressure /
    design_temp) on top of whatever the saved payload had. Also stamps
    rating / material / corrosion_allowance / mdmt_c / joint_type at the
    top level so the Excel download route can read them without digging
    into nested sub-dicts."""
    out = dict(snapshot)
    def _pick(*vals):
        for v in vals:
            if v not in (None, ""):
                return v
        return None

    # Three revision-editable knobs
    out["service"] = _pick(req.service, saved.get("service") if saved else None, snapshot.get("service"))
    out["design_pressure_barg"] = _pick(
        req.design_pressure_barg,
        saved.get("design_pressure_barg") if saved else None,
        snapshot.get("design_pressure_barg"),
        (snapshot.get("design_conditions") or {}).get("design_pressure_barg"),
        (snapshot.get("effective_design_conditions") or {}).get("design_pressure_barg"),
    )
    out["design_temp_c"] = _pick(
        req.design_temp_c,
        saved.get("design_temp_c") if saved else None,
        snapshot.get("design_temp_c"),
        (snapshot.get("design_conditions") or {}).get("design_temp_c"),
        (snapshot.get("effective_design_conditions") or {}).get("design_temp_c"),
    )

    # Stamp top-level lookup fields used by the Excel exporter
    out["rating"] = _pick(req.rating, saved.get("rating") if saved else None, snapshot.get("rating"))
    out["material"] = _pick(req.material, saved.get("material") if saved else None, snapshot.get("material"))
    out["corrosion_allowance"] = _pick(
        req.corrosion_allowance,
        saved.get("corrosion_allowance") if saved else None,
        snapshot.get("corrosion_allowance"),
        snapshot.get("digit"),
    )
    out["mdmt_c"] = _pick(
        saved.get("mdmt_c") if saved else None,
        snapshot.get("mdmt_c"),
        (snapshot.get("design_conditions") or {}).get("mdmt_c"),
        (snapshot.get("effective_design_conditions") or {}).get("mdmt_c"),
    )
    out["joint_type"] = _pick(
        saved.get("joint_type") if saved else None,
        snapshot.get("joint_type"),
        (snapshot.get("design_conditions") or {}).get("joint_type"),
        (snapshot.get("effective_design_conditions") or {}).get("joint_type"),
    )
    return out


# ============================================================================
# Routes
# ============================================================================
@router.get("/state-machine")
def state_machine(_user=Depends(get_current_user)):
    return store.state_machine_info()


@router.get("/pms-classes")
def list_pms_classes(_user=Depends(get_current_user)) -> list:
    """Return distinct saved PMS classes with their rating / material /
    corrosion_allowance so the workflow Create page can offer a class
    picker instead of showing those fields as raw dropdowns."""
    try:
        with session_store.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (piping_class)
                    piping_class, rating, material, corrosion_allowance
                FROM saved_pms
                ORDER BY piping_class, updated_at DESC
                """
            )
            rows = cur.fetchall()
        return [
            {
                "piping_class": r[0],
                "rating": r[1],
                "material": r[2],
                "corrosion_allowance": r[3],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("pms-classes lookup failed: %s", exc)
        return []


@router.get("/options")
def options(_user=Depends(get_current_user)) -> dict:
    """Pass-through to the existing /api/options/all so the workflow
    Create page doesn't need to know about both base URLs. Returns
    {pressure_ratings, materials, corrosion_allowances, services, …}.

    Same shape as `options_routes.all_options` — re-rendered here as a
    cross-namespace alias so the frontend's PMS Workflow client only
    needs the `VITE_PMS_API_URL` env var.
    """
    from app.routes.options_routes import all_options as _all_options
    return _all_options()


@router.get("/workflows")
def list_workflows(
    project_id: Optional[str] = None,
    piping_class: Optional[str] = None,
    _user=Depends(get_current_user),
):
    try:
        return store.list_workflows(project_id=project_id, piping_class=piping_class)
    except StoreError as exc:
        raise _store_to_http(exc)


@router.post("/workflows", status_code=201)
def create_workflow(req: WorkflowCreate, user=Depends(get_current_user)):
    """Fast-path: when piping_class is supplied and a saved_pms row exists,
    use the stored payload directly (no re-generation). This is the normal
    path from the Create page — the user picked a class from the saved-class
    picker, so the payload is already on disk.

    Slow-path fallback: if no saved row exists, call the pms_snapshot engine
    (requires rating + material + corrosion_allowance). This handles the
    edge case where someone creates a workflow for a class that was never
    saved via the PMS Generator UI."""

    # ── Fast path: saved class provided ──────────────────────────────
    if req.piping_class:
        saved = _lookup_saved_pms_payload(req.piping_class)
        if saved and saved.get("payload"):
            base_payload = dict(saved["payload"])
            resolved_class = req.piping_class
            snapshot_payload = _seed_editable(base_payload, req, saved)
        else:
            # Class name given but no saved row — fall back to generation.
            base_payload = _generate_snapshot(req)
            resolved_class = req.piping_class
            snapshot_payload = _seed_editable(base_payload, req, None)
    else:
        # ── Slow path: derive class from rating/material/CA ───────────
        base_payload = _generate_snapshot(req)
        resolved_class = (
            base_payload.get("class_code") or base_payload.get("base_class_code")
        )
        if not resolved_class:
            raise HTTPException(
                status_code=422,
                detail="Could not resolve a piping class from the supplied inputs.",
            )
        saved = _lookup_saved_pms_payload(resolved_class)
        if saved and saved.get("payload"):
            base_payload = dict(saved["payload"])
        snapshot_payload = _seed_editable(base_payload, req, saved)

    title = (
        req.document_title
        or f"PMS Datasheet — {resolved_class} ({req.project_id})"
    )
    try:
        result = store.create_workflow(
            project_id=req.project_id,
            piping_class=resolved_class,
            document_title=title,
            starting_state="A0",
            snapshot_payload=snapshot_payload,
            user=user,
        )
    except StoreError as exc:
        raise _store_to_http(exc)
    return {**result, "piping_class": resolved_class}


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, _user=Depends(get_current_user)):
    try:
        return store.get_workflow(workflow_id)
    except StoreError as exc:
        raise _store_to_http(exc)


@router.post("/workflows/{workflow_id}/transition", status_code=201)
def transition(workflow_id: str, req: TransitionIn, user=Depends(get_current_user)):
    try:
        return store.transition(
            workflow_id,
            target_state=req.target_state,
            is_rfq=req.is_rfq,
            change_identifiers=[ci.model_dump() for ci in (req.change_identifiers or [])],
            user=user,
        )
    except StoreError as exc:
        raise _store_to_http(exc)


@router.post("/workflows/{workflow_id}/void", status_code=201)
def void_workflow(workflow_id: str, user=Depends(get_current_user)):
    if (user.get("role_code") or "").upper() != "APPROVER":
        raise HTTPException(403, "Only APPROVER can void a workflow.")
    try:
        return store.void_workflow(workflow_id, user)
    except StoreError as exc:
        raise _store_to_http(exc)


@router.get("/workflows/{workflow_id}/audit")
def audit_log(workflow_id: str, _user=Depends(get_current_user)):
    return store.audit(workflow_id)


@router.post("/revisions/{revision_id}/sign", status_code=201)
def sign(revision_id: str, req: SignIn, user=Depends(get_current_user)):
    try:
        return store.sign(
            revision_id,
            signature_type=req.signature_type,
            decision=req.decision,
            comment=req.comment,
            user=user,
        )
    except StoreError as exc:
        raise _store_to_http(exc)


@router.get("/revisions/{revision_id}/snapshot")
def get_snapshot(revision_id: str, _user=Depends(get_current_user)):
    try:
        snap = store.get_snapshot(revision_id)
        return {
            "revision_id": snap["revision_id"],
            "payload": snap["payload"],
            "has_cached_excel": snap.get("cached_excel_bytes") is not None,
            "created_at": snap.get("created_at"),
            "updated_at": snap.get("updated_at"),
        }
    except StoreError as exc:
        raise _store_to_http(exc)


@router.post("/revisions/{revision_id}/snapshot", status_code=201)
def upsert_snapshot(revision_id: str, req: SnapshotIn, user=Depends(get_current_user)):
    try:
        store.upsert_snapshot(
            revision_id, req.payload or {}, user,
            full_replace=req.full_replace,
        )
    except StoreError as exc:
        raise _store_to_http(exc)
    return {"ok": True}


@router.get("/revisions/{revision_id}/download")
def download_revision(revision_id: str, _user=Depends(get_current_user)):
    """Render the revision's frozen snapshot as an xlsx.

    Uses the stored payload directly — does NOT re-run the PMS engine.
    This means any edits the user made to service / design-pressure /
    design-temp after creation are reflected in the downloaded file.
    """
    try:
        snap = store.get_snapshot(revision_id)
    except StoreError as exc:
        raise _store_to_http(exc)

    payload = snap["payload"] or {}

    # Look up revision code + signatures for the sheet.
    rev_code   = "A0"
    signatures = []
    try:
        rev_info   = store.get_revision(revision_id)
        rev_code   = (rev_info or {}).get("code", "A0")
        signatures = store.get_signatures(revision_id)
    except Exception as exc:
        logger.warning("Could not fetch revision/signatures for %s: %s", revision_id, exc)

    try:
        wb, class_code = excel_exporter.build_workbook_from_snapshot(
            payload, rev_code, signatures=signatures
        )
    except Exception as exc:
        raise HTTPException(500, f"Excel render failed: {exc}")

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    filename = f"PMS_{class_code}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
