"""build_pms_snapshot — the single source of truth for everything the
SPA renders.

Default-design-condition policy when the caller omits them:
  1. If the class has a P-T envelope in pt_tables.json, seed both
     pressure and temperature from `hottest_point` so the design point
     lies on the curve → adequacy banner shows ADEQUATE.
  2. If no P-T entry (CuNi at 150# is the only catalogued case today),
     fall back to a rating-derived pressure (so the WT engine has
     something to compute against) + 50 °C as a generic design temp.
     The defaults table is in `_RATING_FALLBACK_PRESSURES_BARG` below.
  3. MDMT and joint default to project-standard values (-29 °C / Seamless).

Aggregates class_resolver + wt_calc into one dict that contains:
  • class identification (code, letter, digit, suffix)
  • pressure-temperature curve
  • code factors (stress table, Y curve, fitting specs, flange extras,
    branch chart)
  • adequacy banner (Tab 1)
  • derived design conditions — hydrotest, operating 80%, °F (Tab 1)
  • wall thickness rows + summary + flags + formula example (Tab 2)
  • materials_tab — small-bore + large-bore component tables (Tab 3)
  • design_conditions snapshot of the user-picked operating point

Both `POST /api/compute-pms` (live UI) and `POST /api/pms-agent/save`
(persist with snapshot) call this — guaranteeing they produce the
same numbers from the same inputs.

The frontend MUST NOT recompute any of these values. The SPA's job is
to render this dict; the engineering logic lives here.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services import class_resolver, wt_calc

logger = logging.getLogger(__name__)


import re as _re

# Defaults used when the caller omits a design condition.
_DEFAULT_MDMT_C   = -29.0
_DEFAULT_JOINT    = "Seamless"
# Generic fallback design T when neither the caller nor the P-T table
# provides one (e.g. CuNi at 150# has no indexed P-T curve in the
# project's pt_tables.json).
_FALLBACK_DESIGN_T_C = 50.0
# Rating → nominal flange P (barg) — ASME B16.5 group 1.1 cold-end
# pressures. Used when the material has no indexed P-T envelope so
# the WT engine still has a pressure to compute against.
_RATING_FALLBACK_PRESSURES_BARG = {
    "150#":   19.6,
    "300#":   51.1,
    "600#":  102.1,
    "900#":  153.2,
    "1500#": 255.3,
    "2500#": 425.5,
    "5000#": 851.0,
    "10000#": 1702.0,
}


def _rating_fallback_pressure_barg(rating: Optional[str]) -> Optional[float]:
    if not rating:
        return None
    # Strip trailing "Tubing A/B/C" variants — they share the rating's letter.
    base = (rating or "").strip()
    if _RATING_FALLBACK_PRESSURES_BARG.get(base) is not None:
        return _RATING_FALLBACK_PRESSURES_BARG[base]
    # Tubing / EEMUA without B16.5 fallback — use a small default so the
    # calc can still produce something.
    return 20.0


def _seed_defaults(
    pt: Optional[dict],
    rating: Optional[str],
    design_pressure_barg: Optional[float],
    design_temp_c: Optional[float],
    mdmt_c: Optional[float],
    joint_type: Optional[str],
) -> dict:
    """Fill in sensible defaults when the caller didn't provide design
    conditions.

    Path A (typical) — P-T curve indexed for this material: seed both
        pressure and temperature from `hottest_point` so the design
        point lies on the curve (ADEQUATE banner by default).

    Path B (CuNi-style) — material has no P-T entry: fall back to the
        rating's nominal flange pressure + a generic 50 °C design T.
        The WT engine then has real numbers to compute against; the
        adequacy banner stays None (we can't verify against a missing
        curve). The user can still override the inputs in the UI."""
    hottest = (pt or {}).get("hottest_point") or {}

    seeded_p = design_pressure_barg
    if seeded_p is None:
        seeded_p = hottest.get("pressure_barg")
    if seeded_p is None:
        # No P-T curve at all — fall back on the rating's cold-end pressure.
        seeded_p = _rating_fallback_pressure_barg(rating)

    seeded_t = design_temp_c
    if seeded_t is None:
        seeded_t = hottest.get("temperature_c")
    if seeded_t is None:
        seeded_t = _FALLBACK_DESIGN_T_C

    return {
        "design_pressure_barg": seeded_p,
        "design_temp_c":        seeded_t,
        "mdmt_c":     mdmt_c if mdmt_c is not None else _DEFAULT_MDMT_C,
        "joint_type": joint_type or _DEFAULT_JOINT,
    }


def build_pms_snapshot(
    *,
    rating: str,
    material: str,
    corrosion_allowance: str,
    service: str = "",
    design_pressure_barg: Optional[float] = None,
    design_temp_c: Optional[float] = None,
    mdmt_c: Optional[float] = None,
    joint_type: Optional[str] = None,
) -> dict:
    """Produce the canonical full-PMS snapshot for the given inputs.

    Raises `class_resolver.ResolutionError` when the inputs don't fit
    the §5.5 rules — callers should map this to a 422.

    Returns a dict with the same shape stored in `saved_pms.payload`,
    plus an `effective_design_conditions` block so the SPA knows what
    defaults were applied when it sent nulls."""
    resolved = class_resolver.resolve(
        rating=rating, material=material, ca=corrosion_allowance, service=service or "",
    )
    pt = resolved.get("pressure_temperature")
    eff = _seed_defaults(pt, rating, design_pressure_barg, design_temp_c, mdmt_c, joint_type)

    # Compute every derived block. Best-effort: snapshot survives even
    # when wt_calc raises (e.g. composite material with no B16.5 stress
    # data) — the affected sections render empty in the UI.
    try:
        wt_rows = wt_calc.compute_wall_thickness_rows(
            resolved=resolved,
            material=material,
            ca=corrosion_allowance,
            design_pressure_barg=eff["design_pressure_barg"],
            design_temp_c=eff["design_temp_c"],
            joint_type=eff["joint_type"],
            service=service or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.compute_wall_thickness_rows failed: %s", e)
        wt_rows = []

    try:
        wt_summary = wt_calc.summary_stats(
            wt_rows, pt, eff["design_pressure_barg"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.summary_stats failed: %s", e)
        wt_summary = {}

    try:
        flags = wt_calc.evaluate_flags(
            resolved=resolved,
            material=material,
            design_temp_c=eff["design_temp_c"],
            mdmt_c=eff["mdmt_c"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.evaluate_flags failed: %s", e)
        flags = []

    try:
        formula_example = wt_calc.build_formula_example(
            resolved=resolved,
            material=material,
            ca=corrosion_allowance,
            design_pressure_barg=eff["design_pressure_barg"],
            design_temp_c=eff["design_temp_c"],
            joint_type=eff["joint_type"],
            service=service or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.build_formula_example failed: %s", e)
        formula_example = None

    try:
        materials_tab = wt_calc.build_materials_snapshot(resolved, wt_rows)
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.build_materials_snapshot failed: %s", e)
        materials_tab = None

    try:
        adequacy_check = wt_calc.adequacy(
            pt, eff["design_pressure_barg"], eff["design_temp_c"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.adequacy failed: %s", e)
        adequacy_check = None

    try:
        derived_conditions = wt_calc.derived_design_conditions(
            pt=pt,
            design_pressure_barg=eff["design_pressure_barg"],
            design_temp_c=eff["design_temp_c"],
            mdmt_c=eff["mdmt_c"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("wt_calc.derived_design_conditions failed: %s", e)
        derived_conditions = {}

    return {
        # Pass through the entire resolve-class output (class_code,
        # letter, digit, suffix, trailing, service, note,
        # pressure_temperature, code_factors).
        **resolved,
        # Snapshot of the user-picked operating point (may include
        # defaults when the caller omitted any field).
        "design_conditions": {
            "design_pressure_barg": eff["design_pressure_barg"],
            "design_temp_c":        eff["design_temp_c"],
            "mdmt_c":               eff["mdmt_c"],
            "joint_type":           eff["joint_type"],
        },
        # What the SPA's input boxes should display after this call
        # (separate from `design_conditions` so the SPA can tell
        # whether server-side defaults were applied).
        "effective_design_conditions": eff,
        # Tab 1
        "adequacy":          adequacy_check,
        "derived_conditions": derived_conditions,
        # Tab 2
        "wall_thickness": {
            "rows":            wt_rows,
            "summary":         wt_summary,
            "flags":           flags,
            "formula_example": formula_example,
        },
        # Tab 3 — branch chart already lives in
        # `code_factors.branch_chart` inside the spread-in resolved data.
        "materials_tab": materials_tab,
    }
