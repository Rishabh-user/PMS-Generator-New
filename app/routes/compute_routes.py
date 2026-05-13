"""POST /api/compute-pms — live snapshot endpoint.

The SPA's PMS Generator page (and the chat-side InlinePMSPreview)
call this whenever the user changes any input — Rating, Material,
Corrosion Allowance, Service, Design P, Design T, MDMT, Joint Type.
The full computed snapshot comes back in one round-trip; the SPA
renders it without doing any math itself.

This is the same `build_pms_snapshot()` function used by
`POST /api/pms-agent/save` for the persistence payload — so save
output and live preview can never drift.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import class_resolver, pms_snapshot


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["compute"])


class ComputePMSRequest(BaseModel):
    rating:               str = Field(..., min_length=1)
    material:             str = Field(..., min_length=1)
    corrosion_allowance:  str = Field(..., min_length=1)
    service:              str = ""
    # All design conditions are optional. When omitted the backend
    # seeds them from the class's P-T envelope (hottest_point).
    design_pressure_barg: Optional[float] = None
    design_temp_c:        Optional[float] = None
    mdmt_c:               Optional[float] = None
    joint_type:           Optional[str] = None


@router.post("/compute-pms")
def compute_pms(req: ComputePMSRequest) -> dict:
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
    except class_resolver.ResolutionError as e:
        # User input space — not a server fault.
        raise HTTPException(status_code=422, detail=str(e)) from e
