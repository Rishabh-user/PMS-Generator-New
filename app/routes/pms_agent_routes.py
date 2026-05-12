"""PMS-Agent API surface — chat, session history, and chat-driven Excel
downloads.

Routes mirror the old pms-generator backend so the existing PMSAgentPage
in SPE-Valvesheet-Frontend keeps working with only an API-base URL change.

Endpoints:
    POST   /api/pms-agent/chat                  — slot-filling chat
    GET    /api/pms-agent/sessions              — list current user's chats
    GET    /api/pms-agent/sessions/{id}         — fetch one
    PUT    /api/pms-agent/sessions/{id}         — create / overwrite
    PATCH  /api/pms-agent/sessions/{id}         — rename
    DELETE /api/pms-agent/sessions/{id}         — delete
    POST   /api/pms-agent/download-excel        — single PMS, defaults applied
    POST   /api/pms-agent/download-zip          — bulk ZIP of multiple PMSes

The session endpoints are scoped by the `X-User-Id` header — same trust
model as the old backend (no server-side auth; the SPA is trusted).
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import (
    class_resolver,
    excel_exporter,
    pms_agent_service,
    session_store,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pms-agent", tags=["pms-agent"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP_MIME = "application/zip"


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatHistoryTurn(BaseModel):
    role:    str
    content: str


class ChatRequest(BaseModel):
    prompt:  str = Field(..., min_length=1)
    history: list[ChatHistoryTurn] = Field(default_factory=list)


@router.post("/chat")
def chat(req: ChatRequest) -> dict:
    history_dicts = [t.model_dump() for t in req.history]
    return pms_agent_service.chat(req.prompt, history_dicts)


# ---------------------------------------------------------------------------
# Session history (X-User-Id scoped, SQLite-backed)
# ---------------------------------------------------------------------------

def _require_user(x_user_id: Optional[str]) -> str:
    if not x_user_id or not x_user_id.strip():
        # Match the old backend's behaviour: missing header → 503 so the UI
        # shows "history sync off" instead of "no chats yet".
        raise HTTPException(
            status_code=503,
            detail="X-User-Id header missing — chat history disabled.",
        )
    return x_user_id.strip()


class SessionUpsertRequest(BaseModel):
    title:                str = "New chat"
    blocks:               list = Field(default_factory=list)
    message_count:        int = 0
    last_message_preview: str = ""


class SessionPatchRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.get("/sessions")
def list_sessions(x_user_id: Optional[str] = Header(default=None)) -> list[dict]:
    user_id = _require_user(x_user_id)
    return session_store.list_sessions(user_id)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, x_user_id: Optional[str] = Header(default=None)) -> dict:
    user_id = _require_user(x_user_id)
    row = session_store.get_session(user_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


@router.put("/sessions/{session_id}")
def upsert_session(
    session_id: str,
    req: SessionUpsertRequest,
    x_user_id: Optional[str] = Header(default=None),
) -> dict:
    user_id = _require_user(x_user_id)
    session_store.upsert_session(
        user_id=user_id,
        session_id=session_id,
        title=req.title or "New chat",
        blocks=req.blocks,
        message_count=req.message_count,
        last_preview=req.last_message_preview,
    )
    return {"ok": True}


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str,
    req: SessionPatchRequest,
    x_user_id: Optional[str] = Header(default=None),
) -> dict:
    user_id = _require_user(x_user_id)
    if not session_store.rename_session(user_id, session_id, req.title):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, x_user_id: Optional[str] = Header(default=None)) -> dict:
    user_id = _require_user(x_user_id)
    if not session_store.delete_session(user_id, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat-driven Excel downloads
#
# The chat UI surfaces match cards with a single (rating, material, CA,
# service) tuple per card. The deterministic Excel exporter still needs
# design_p / design_t / mdmt / joint to do its B31.3 maths — for the chat
# flow we apply sensible defaults from pt_lookup.cold_point. Engineers
# who want non-default conditions use the AI Agent's chat to express
# design_pressure / design_temperature, which Claude extracts into the
# `interpreted` block; the frontend can pass those back in to override.
# ---------------------------------------------------------------------------

class ChatClassRequest(BaseModel):
    piping_class:        str
    rating:              Optional[str] = None
    material:            str
    corrosion_allowance: str
    service:             str = ""
    design_pressure_barg: Optional[float] = None
    design_temp_c:       Optional[float] = None
    mdmt_c:              Optional[float] = None
    joint_type:          Optional[str] = None


class ChatBulkRequest(BaseModel):
    classes: list[ChatClassRequest]


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "PMS"


def _resolve_rating(req: ChatClassRequest) -> str:
    """The chat match cards include `rating`; if a caller omits it we
    derive it by trying each catalog rating until class_resolver picks
    the same class code. This keeps the contract permissive."""
    if req.rating:
        return req.rating
    from app.services.pms_agent_service import _catalog  # local to avoid cycle
    for r in _catalog()["ratings"]:
        try:
            resolved = class_resolver.resolve(
                rating=r, material=req.material,
                ca=req.corrosion_allowance, service=req.service or "",
            )
            if resolved["class_code"] == req.piping_class:
                return r
        except class_resolver.ResolutionError:
            continue
    raise HTTPException(
        status_code=422,
        detail=f"Could not derive rating for class {req.piping_class}.",
    )


def _build_excel(req: ChatClassRequest) -> tuple[io.BytesIO, str]:
    rating = _resolve_rating(req)
    defaults = pms_agent_service.default_design_conditions(rating, req.material)
    p = req.design_pressure_barg if req.design_pressure_barg is not None else defaults["design_p_barg"]
    t = req.design_temp_c if req.design_temp_c is not None else defaults["design_t_c"]
    mdmt = req.mdmt_c if req.mdmt_c is not None else defaults["mdmt_c"]
    joint = req.joint_type or defaults["joint_type"]

    try:
        return excel_exporter.build_workbook(
            rating=rating,
            material=req.material,
            ca=req.corrosion_allowance,
            service=req.service or "",
            design_p_barg=p,
            design_t_c=t,
            mdmt_c=mdmt,
            joint_type=joint,
        )
    except class_resolver.ResolutionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/download-excel")
def download_excel(req: ChatClassRequest):
    buf, filename = _build_excel(req)
    return StreamingResponse(
        buf,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "no-store",
        },
    )


@router.post("/download-zip")
def download_zip(req: ChatBulkRequest):
    if not req.classes:
        raise HTTPException(status_code=422, detail="No classes provided.")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for class_req in req.classes:
            try:
                excel_buf, filename = _build_excel(class_req)
            except HTTPException:
                # Skip individual failures so a single bad class doesn't
                # kill the bulk request — but log it.
                logger.warning("Bulk: skipping %s (resolve failed)", class_req.piping_class)
                continue
            zf.writestr(filename, excel_buf.getvalue())

    zip_buf.seek(0)
    archive_name = f"PMS_Bulk_{len(req.classes)}_classes.zip"
    return StreamingResponse(
        zip_buf,
        media_type=ZIP_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{archive_name}"',
            "Cache-Control":       "no-store",
        },
    )
