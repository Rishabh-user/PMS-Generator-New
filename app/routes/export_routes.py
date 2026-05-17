"""Export routes — stream a PMS datasheet to the user as either an
.xlsx workbook or a PDF rendered from the same workbook.

Both endpoints accept the identical request shape (rating / material /
ca / service / design conditions / joint type) so the SPA can swap one
URL for the other without rebuilding the payload."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services import class_resolver, excel_exporter, pdf_exporter


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
PDF_MIME  = "application/pdf"


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


@router.post("/pdf")
def export_pdf(req: ExcelRequest):
    """PDF rendering of the same datasheet. Reuses /excel's request
    shape so the SPA only has to swap the URL. The PDF is produced by
    converting the canonical .xlsx via LibreOffice headless mode — see
    `pdf_exporter.build_pdf` for the dependency setup."""
    try:
        buf, filename = pdf_exporter.build_pdf(
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
    except pdf_exporter.LibreOfficeMissingError as e:
        # 503 — the service is temporarily/permanently unavailable on this
        # host, but the rest of the API works. Surface a helpful install
        # hint so the operator can fix it without reading the logs.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except pdf_exporter.LibreOfficeConversionError as e:
        # 500 — LibreOffice ran but the conversion didn't succeed.
        raise HTTPException(status_code=500, detail=str(e)) from e

    return StreamingResponse(
        buf,
        media_type=PDF_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "no-store",
        },
    )
