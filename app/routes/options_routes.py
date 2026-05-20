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
    (re.compile(r"(?i)\bTITANIUM\b|\bTi\b|B861"),   "nps_dimensions_titanium.json"),
    # Tubing materials — SS 316/316L Tubing (digit 80) and 6 MO Tubing
    # (digit 90). Use 4-NPS instrument-tubing dimensions per ASTM A 269.
    (re.compile(r"(?i)Tubing|N08367|6\s*MO"),       "nps_dimensions_tubing.json"),
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
    # Cached per-process. Restart uvicorn (or touch a .py file the
    # server imports) when the underlying JSON files change in dev.
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


def _compute_rating_restrictions(ratings_doc: dict, materials_doc: dict) -> dict:
    """Resolve `class_naming.rating_restrictions` into UI-friendly form
    so the SPA can drive its dropdowns without re-implementing the §5.5
    digit logic.

    Input form (in class_naming.json → rating_restrictions):
        low_pressure_only_digits  — material digits restricted to low-P
        low_pressure_only_letter  — the letter those ratings share
        tubing_letter             — letter for Tubing A/B/C series
        tubing_only_digits        — material digits valid for tubing ratings

    Output form (what /api/options/all returns):
        rating_restrictions: {
            # Material → rating direction (exotic materials → 150#/EEMUA only)
            restricted_materials: ["Copper", "CuNi (Valve: NAB)", …],
            allowed_ratings:      ["150#", "EEMUA 20 bar"],
            message:              "This material is only catalogued at …",

            # Rating → material direction (Tubing series → tubing materials only)
            tubing_only_ratings:   ["Tubing A", "Tubing B", "Tubing C"],
            tubing_only_materials: ["SS 316 / 316L (Tubing)", "6 MO Tubing"],
            tubing_only_message:   "Tubing ratings only support …",
        }

    The SPA uses these to disable invalid (rating, material) pairs in
    BOTH dropdowns up front. Backend stays the single source of truth —
    change the JSON, restart, and the SPA picks it up on the next fetch.
    """
    naming = _load("class_naming.json")
    rr = naming.get("rating_restrictions") or {}
    rating_letters = naming.get("rating_letters") or {}
    all_ratings = ratings_doc.get("ratings") or []
    ui_labels = materials_doc.get("materials") or []

    def _strip_parens(s: str) -> str:
        return re.sub(r"\s*\(.*?\)\s*", "", s or "").strip()

    # Build {cleaned_label → original_label} so we can map a digit
    # rule's `material` field back to the exact dropdown string.
    # (class_naming.material_digits keys on the cleaned, paren-free
    # form; the UI dropdown shows the label form.)
    cleaned_to_label: dict[str, str] = {}
    for lbl in ui_labels:
        cleaned_to_label.setdefault(_strip_parens(lbl).upper(), lbl)

    def _ui_labels_for_digits(digit_set: set[str]) -> list[str]:
        """Walk `material_digits` and return the UI labels whose digit
        is in `digit_set`. Stable order preserved from the JSON file."""
        out: list[str] = []
        seen: set[str] = set()
        for rule in naming.get("material_digits") or []:
            if rule.get("digit") not in digit_set:
                continue
            rule_mat = _strip_parens(rule.get("material") or "").upper()
            ui_label = cleaned_to_label.get(rule_mat)
            if ui_label and ui_label not in seen:
                out.append(ui_label)
                seen.add(ui_label)
        return out

    # ── Rule 1: exotic materials → low-pressure ratings only ──
    restricted_digits = set(rr.get("low_pressure_only_digits") or [])
    allowed_letter = rr.get("low_pressure_only_letter", "A")
    restricted_labels = _ui_labels_for_digits(restricted_digits) if restricted_digits else []
    allowed_ratings = [r for r in all_ratings if rating_letters.get(r) == allowed_letter]
    msg1 = (
        "This material is only catalogued at "
        f"{', '.join(allowed_ratings) or allowed_letter}. "
        "Pick a different rating or material."
    ) if restricted_labels else ""

    # ── Rule 2: tubing ratings → instrument-tubing materials only ──
    tubing_letter = rr.get("tubing_letter")
    tubing_only_digits = set(rr.get("tubing_only_digits") or [])
    tubing_only_ratings = (
        [r for r in all_ratings if rating_letters.get(r) == tubing_letter]
        if tubing_letter else []
    )
    tubing_only_materials = (
        _ui_labels_for_digits(tubing_only_digits) if tubing_only_digits else []
    )
    msg2 = (
        f"{', '.join(tubing_only_ratings)} are instrument-tubing series — "
        f"only tubing materials apply ({', '.join(tubing_only_materials)})."
    ) if (tubing_only_ratings and tubing_only_materials) else ""

    return {
        # Material → rating
        "restricted_materials": restricted_labels,
        "allowed_ratings":      allowed_ratings,
        "message":              msg1,
        # Rating → material
        "tubing_only_ratings":   tubing_only_ratings,
        "tubing_only_materials": tubing_only_materials,
        "tubing_only_message":   msg2,
    }


@router.get("/options/all")
def all_options() -> dict:
    """One round-trip for the form to populate every dropdown at once.

    `disabled_pressure_ratings` is the subset of `pressure_ratings`
    that should be rendered as disabled (visible but unselectable).
    `*_categories` are optional parallel metadata for UIs that want
    to render `<optgroup>` headers. Every decision lives in the JSON
    files — re-enable / re-group / reorder is a JSON edit. No code
    change anywhere.

    `rating_restrictions` exposes the same project rule that the
    class_resolver enforces server-side: certain "exotic" materials
    (Copper / Titanium / GRE / CPVC / CuNi) only have catalogued
    classes at low-pressure ratings (150# / EEMUA 20 bar). The SPA
    uses this to grey-out invalid (material, rating) pairs in its
    dropdowns before the user clicks "Generate". The single source of
    truth lives in `class_naming.json` — see
    `_compute_rating_restrictions` for the resolution rules.
    """
    ratings_doc = _load("pressure_ratings.json")
    materials_doc = _load("materials.json")
    services_doc = _load("services.json")
    return {
        "pressure_ratings": ratings_doc["ratings"],
        "disabled_pressure_ratings": ratings_doc.get("disabled_ratings", []),
        "pressure_ratings_categories": ratings_doc.get("categories", []),
        "materials": materials_doc["materials"],
        "materials_categories": materials_doc.get("categories", []),
        "corrosion_allowances": _load("corrosion_allowances.json")["corrosion_allowances"],
        "services": services_doc["services"],
        "services_categories": services_doc.get("categories", []),
        "services_allow_custom": services_doc.get("allow_custom", True),
        "rating_restrictions": _compute_rating_restrictions(ratings_doc, materials_doc),
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
