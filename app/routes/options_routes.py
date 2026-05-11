"""Read-only endpoints that serve dropdown lists out of /app/data/*.json.

The four files are the single source of truth for the Step 1 form. Editing a
JSON file and refreshing the browser is enough — no code changes needed."""
import json
import re
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.config import settings


router = APIRouter(prefix="/api", tags=["options"])


# ---------------------------------------------------------------------------
# Per-material NPS dimension overrides
# ---------------------------------------------------------------------------
# Most materials follow ASME B36.10M OD values (in `nps_dimensions.json`).
# A few — notably 90/10 CuNi — follow EEMUA 144 with different ODs at small
# bores. Add a new override here when another material family diverges.
_NPS_OVERRIDES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\bCuNi\b|C70600|B466"),       "nps_dimensions_cuni.json"),
    # Copper must come AFTER CuNi (CuNi contains 'Cu' but isn't generic copper).
    (re.compile(r"(?i)\bCOPPER\b|C12200|\bB42\b"),  "nps_dimensions_copper.json"),
    (re.compile(r"(?i)\bCPVC\b"),                   "nps_dimensions_cpvc.json"),
]


_GRE_MATERIAL_PATTERN = re.compile(r"(?i)\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced")
_BONSTRAND_SERVICE_PATTERN = re.compile(r"(?i)Hypochlorite|BONSTRAND")


def _resolve_nps_file(material: Optional[str], service: Optional[str] = None) -> str:
    if not material:
        return "nps_dimensions.json"
    # GRE has two sub-catalogs distinguished by service:
    #   Hypochlorite / BONSTRAND  → A51 (6 NPS sizes, manufacturer catalog)
    #   Anything else (Raw Sea Water, Special) → A50/A52 (20 NPS sizes)
    if _GRE_MATERIAL_PATTERN.search(material):
        if service and _BONSTRAND_SERVICE_PATTERN.search(service):
            return "nps_dimensions_gre_bonstrand.json"
        return "nps_dimensions_gre.json"
    for pat, fname in _NPS_OVERRIDES:
        if pat.search(material):
            return fname
    return "nps_dimensions.json"


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
def nps_dimensions(material: Optional[str] = None, service: Optional[str] = None) -> dict:
    """NPS → OD (mm) lookup. Material-aware (CuNi / Copper / GRE override
    the default B36.10M list) and for GRE also service-aware (Hypochlorite
    routes to the BONSTRAND catalog with 6 NPS sizes)."""
    return _load(_resolve_nps_file(material, service))


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
