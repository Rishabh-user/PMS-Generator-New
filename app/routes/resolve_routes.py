"""POST /api/resolve-class — turn a (rating, material, CA, service) selection
into a §5.5 class code."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import class_resolver, wt_calc


router = APIRouter(prefix="/api", tags=["resolve"])


class ResolveRequest(BaseModel):
    rating:               str = Field(..., min_length=1)
    material:             str = Field(..., min_length=1)
    corrosion_allowance:  str = Field(..., min_length=1)
    service:              Optional[str] = ""


@router.post("/resolve-class")
def resolve_class(req: ResolveRequest) -> dict:
    try:
        resolved = class_resolver.resolve(
            rating=req.rating,
            material=req.material,
            ca=req.corrosion_allowance,
            service=req.service,
        )
    except class_resolver.ResolutionError as e:
        # 422 — user-input space, not a server fault.
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Project standard notes (the "NOTES" section that appears on the
    # PMS Excel datasheet). Filter by the inputs we have here — the
    # snapshot path applies the same filter using effective design T;
    # since we don't have a design T at this stage we pass None, which
    # means notes gated on temperature only fire later via compute-pms.
    resolved["project_notes"] = wt_calc.resolve_project_notes(
        rating=req.rating,
        material=req.material,
        service=(req.service or None),
    )
    return resolved
