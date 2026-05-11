"""Read-only endpoints that serve dropdown lists out of /app/data/*.json.

The four files are the single source of truth for the Step 1 form. Editing a
JSON file and refreshing the browser is enough — no code changes needed."""
import json
from functools import lru_cache

from fastapi import APIRouter, HTTPException

from app.config import settings


router = APIRouter(prefix="/api", tags=["options"])


@lru_cache(maxsize=8)
def _load(filename: str) -> dict:
    path = settings.data_dir / filename
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Missing data file: {filename}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/options/pressure-ratings")
def pressure_ratings() -> dict:
    return _load("pressure_ratings.json")


@router.get("/options/materials")
def materials() -> dict:
    return _load("materials.json")


@router.get("/options/corrosion-allowances")
def corrosion_allowances() -> dict:
    return _load("corrosion_allowances.json")


@router.get("/options/services")
def services() -> dict:
    return _load("services.json")


@router.get("/options/all")
def all_options() -> dict:
    """One round-trip for the form to populate every dropdown at once."""
    return {
        "pressure_ratings": _load("pressure_ratings.json")["ratings"],
        "materials": _load("materials.json")["materials"],
        "corrosion_allowances": _load("corrosion_allowances.json")["corrosion_allowances"],
        "services": _load("services.json")["services"],
        "services_allow_custom": _load("services.json").get("allow_custom", True),
    }


@router.get("/nps-dimensions")
def nps_dimensions() -> dict:
    """NPS → OD (mm) lookup used by the Wall Thickness Calculation Table.
    Same list for every PMS class — fetched once on report load."""
    return _load("nps_dimensions.json")


@router.get("/pipe-dimensions")
def pipe_dimensions() -> dict:
    """Full ASME B36.10M Table 2-1 (NPS x Schedule x OD x WT). Powers
    the dynamic SCH / SEL. THK selection in the Wall Thickness Table —
    per B36.10M §9, lightest WT ≥ computed Calc.Thk wins."""
    return _load("pipe_dimensions_b3610.json")


@router.get("/pipe-dimensions-ss")
def pipe_dimensions_ss() -> dict:
    """ASME B36.19M Table 2-1 — stainless schedules (5S, 10S, 40S, 80S).
    Frontend uses this in place of the carbon-steel table when the
    material is stainless / austenitic."""
    return _load("pipe_dimensions_b3619.json")
