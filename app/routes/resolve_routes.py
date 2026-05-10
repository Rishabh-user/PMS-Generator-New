"""POST /api/resolve-class — turn a (rating, material, CA, service) selection
into a §5.5 class code and report whether it's catalogued in the Excel."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import class_resolver


router = APIRouter(prefix="/api", tags=["resolve"])


class ResolveRequest(BaseModel):
    rating:               str = Field(..., min_length=1)
    material:             str = Field(..., min_length=1)
    corrosion_allowance:  str = Field(..., min_length=1)
    service:              Optional[str] = ""


@router.post("/resolve-class")
def resolve_class(req: ResolveRequest) -> dict:
    try:
        return class_resolver.resolve(
            rating=req.rating,
            material=req.material,
            ca=req.corrosion_allowance,
            service=req.service,
        )
    except class_resolver.ResolutionError as e:
        # 422 instead of 500 — this is user-input space, not a server fault.
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/catalogue")
def catalogue() -> dict:
    """Read-only dump of every catalogued class. Useful for debugging
    and for the future Browse-Classes UI."""
    return {"classes": class_resolver._catalogue()}  # noqa: SLF001
