"""AI-augmented endpoints — currently just PMS engineering notes.

All endpoints return a structured `{ok: bool, ...}` shape so the frontend
can render gracefully when the API key isn't configured."""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import ai_service


router = APIRouter(prefix="/api/ai", tags=["ai"])


class NotesRequest(BaseModel):
    class_code:           str
    rating:               str
    material:             str
    ca:                   str
    service:              Optional[str] = ""
    design_p_barg:        Optional[float] = None
    design_t_c:           Optional[float] = None
    mdmt_c:               Optional[float] = None
    joint_type:           Optional[str] = None
    stress_table_label:   Optional[str] = None
    fitting_family:       Optional[str] = None


@router.get("/status")
def ai_status() -> dict:
    """Quick check the UI calls on report load — lets us hide the AI
    Notes button when no key is configured rather than waiting for the
    user to click and see an error."""
    return {"available": ai_service.is_available()}


@router.post("/pms-notes")
def pms_notes(req: NotesRequest) -> dict:
    """Generate AI engineering notes for the resolved PMS state.
    Returns {ok: True, notes: [...], model, usage} on success or
    {ok: False, error: '...'} when the key is missing / call fails."""
    result = ai_service.generate_pms_notes(req.model_dump())
    if "error" in result:
        return {"ok": False, **result}
    return {"ok": True, **result}
