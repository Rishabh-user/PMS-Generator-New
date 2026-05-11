"""POST /api/export/excel — stream an .xlsx PMS datasheet for the
resolved class + design conditions the user is currently viewing."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import class_resolver, excel_exporter


router = APIRouter(prefix="/api/export", tags=["export"])


class ExcelRequest(BaseModel):
    rating:        str
    material:      str
    ca:            str
    service:       Optional[str] = ""
    design_p_barg: float = Field(..., gt=0)
    design_t_c:    float
    mdmt_c:        float = -29
    joint_type:    Optional[str] = "Seamless"


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/excel")
def export_excel(req: ExcelRequest):
    try:
        buf, filename = excel_exporter.build_workbook(
            rating=req.rating,
            material=req.material,
            ca=req.ca,
            service=req.service or "",
            design_p_barg=req.design_p_barg,
            design_t_c=req.design_t_c,
            mdmt_c=req.mdmt_c,
            joint_type=req.joint_type or "Seamless",
        )
    except class_resolver.ResolutionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    return StreamingResponse(
        buf,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "no-store",
        },
    )
