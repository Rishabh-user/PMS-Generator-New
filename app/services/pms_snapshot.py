"""build_pms_snapshot — the single source of truth for everything the
SPA renders.

Default-design-condition policy when the caller omits them:
  1. If the class has a P-T envelope in pt_tables.json, seed
     temperature = `min(hottest_point.T, 300 °C)` and pressure =
     curve-interpolated rated P at that capped temperature. The
     design point lands on the curve → adequacy banner shows
     ADEQUATE. The cap keeps the seeded values near typical
     operating conditions even though our B16.5 curves now extend
     to 538 / 450 / 400 °C; the engineer can still type any T up
     to the curve's published max.
  2. If no P-T entry (CuNi at 150# is the only catalogued case today),
     fall back to a rating-derived pressure (so the WT engine has
     something to compute against) + 50 °C as a generic design temp.
     The defaults table is in `_RATING_FALLBACK_PRESSURES_BARG` below.
  3. MDMT and joint default to project-standard values (-29 °C / Seamless).

The SPA renders Section 2 from `design_conditions_inputs` — a schema
returned by every /api/compute-pms call. Editing
`_build_design_conditions_inputs` below is the single point of change
for adding / removing / reordering form fields.

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
# Cap the seeded default temperature so a freshly-opened page reads
# near typical operating conditions. ASME B16.5 Group 1.1 / 2.3 / 2.8
# curves now extend to 538 / 450 / 400 °C, but defaulting that hot
# makes the seeded pressure tiny. With this cap, the SPA opens at
# 300 °C with the curve-interpolated rated pressure at 300 °C.
_DEFAULT_SEED_TEMP_CAP_C = 300.0

# ── Design-Conditions form schema (drives Section 2 in the SPA) ───
#
# The SPA does NOT hardcode which inputs it renders. It iterates this
# list. To add / remove / reorder a field, edit
# `_build_design_conditions_inputs` below — no SPA change required.
#
# Joint-type options also live here so dropdown contents come from
# the backend. The `e` value (joint efficiency) is consumed by wt_calc;
# the SPA only needs `value` + `label`.
_JOINT_TYPE_OPTIONS: list[dict] = [
    {"value": "Seamless",     "label": "Seamless (E = 1.0)",     "e": 1.0},
    {"value": "EFW, 100% RT", "label": "EFW, 100% RT (E = 1.0)", "e": 1.0},
    {"value": "ERW",          "label": "ERW (E = 0.85)",         "e": 0.85},
    {"value": "EFW",          "label": "EFW (E = 0.85)",         "e": 0.85},
]


def _build_design_conditions_inputs(derived: Optional[dict]) -> list[dict]:
    """Schema describing the inputs the SPA should render in Section 2.

    Only three fields are exposed to the engineer:
      • Design P (barg)
      • Design T (°C)
      • Joint Type

    Design P (psig) is a derived display value (it lives in the
    `derived_conditions` panel below the inputs, not as an editable
    field). MDMT (°C) uses the project default of -29 °C and is not
    user-editable from this form — to override per-class, edit
    `_DEFAULT_MDMT_C` above or extend this schema.

    `col_span` is the layout hint for a 2-column CSS grid on the SPA:
      1 → half width (two such fields pair up on one row)
      2 → full width (spans both columns)
    """
    temp_block = (derived or {}).get("temperature") or {}
    design_f = temp_block.get("design_f")

    # `sync_partner` declares two-way curve sync between paired fields:
    # when the SPA edits a field that has a `sync_partner`, it should
    # null the partner. The next /api/compute-pms call sees the partner
    # is null and interpolates it from the P-T curve (see _seed_defaults
    # above). This lets the SPA do live two-way P ↔ T sync with zero
    # interpolation logic of its own — the curve math lives entirely
    # in pms_snapshot.py + wt_calc.py.
    return [
        {
            "field":         "design_pressure_barg",
            "label":         "Design P (barg)",
            "type":          "number",
            "step":          0.1,
            "min":           0,
            "required":      True,
            "col_span":      1,
            "footnote_text": None,
            "sync_partner":  "design_temp_c",
        },
        {
            "field":         "design_temp_c",
            "label":         "Design T (°C)",
            "type":          "number",
            "step":          1,
            "required":      True,
            "col_span":      1,
            # Pre-formatted °F echo shown directly under the input.
            # Recomputed each /api/compute-pms call so it tracks edits.
            "footnote_text": (
                f"= {design_f:.1f} °F" if isinstance(design_f, (int, float)) else None
            ),
            "sync_partner":  "design_pressure_barg",
        },
        {
            "field":         "joint_type",
            "label":         "Joint Type",
            "type":          "select",
            "required":      False,
            "col_span":      2,
            "options":       [
                {"value": o["value"], "label": o["label"]}
                for o in _JOINT_TYPE_OPTIONS
            ],
            "footnote_text": "ASME B31.3 Table A-1B",
        },
    ]



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

    Seeding rules (when the field is None):
      • design_temp_c        ← `min(hottest_point.T, 300 °C)`.
          The ASME B16.5 Group 1.1 / 2.3 / 2.8 curves extend to 538 /
          450 / 400 °C, but defaulting that hot pulls the rated P way
          down. Capping at 300 °C keeps the seeded operating point
          near typical operating conditions; the engineer can still
          type any T up to the curve's published max.
      • design_pressure_barg ← curve-interpolated rated P **at the
          capped seeded T**. The design point lands exactly on the
          curve → adequacy banner shows ADEQUATE by default.

    Path A (typical) — P-T curve indexed for this material: seed both
        T and P from the curve as above.

    Path B (CuNi-style) — material has no P-T entry: fall back to the
        rating's nominal flange pressure + a generic 50 °C design T.
        The WT engine then has real numbers to compute against; the
        adequacy banner stays None (we can't verify against a missing
        curve). The user can still override the inputs in the UI."""
    hottest = (pt or {}).get("hottest_point") or {}
    temps_curve     = (pt or {}).get("temperatures_c") or []
    pressures_curve = (pt or {}).get("pressures_barg") or []
    has_curve = bool(temps_curve and pressures_curve)

    seeded_t = design_temp_c
    seeded_p = design_pressure_barg

    # Two-way curve sync — drives the SPA's Design P ↔ Design T live
    # auto-sync without any JS interpolation. The SPA simply nulls
    # the OTHER field when one is edited; the backend fills it back
    # in by walking the curve here. Behaviours:
    #   • T given, P null  → P = curve P at T  (existing behaviour)
    #   • P given, T null  → T = curve T at P  (NEW — mirror of above)
    #   • both null        → seed defaults at min(hot_T, 300 °C)
    #   • both given       → pass through as-is (allows off-curve
    #                        adequacy testing — engineer can pick any
    #                        P/T combination to stress-test the class)
    if has_curve and seeded_t is None and seeded_p is not None:
        # P given without T → inverse-interpolate T on the curve.
        seeded_t = wt_calc.interpolate_temperature(temps_curve, pressures_curve, seeded_p)

    # Temperature default (when not derived from P above and not given)
    if seeded_t is None:
        hot_T = hottest.get("temperature_c")
        if hot_T is not None:
            # Cap at 300 °C so the seeded value stays near typical
            # operating conditions even when the curve runs to 538 °C.
            seeded_t = min(float(hot_T), _DEFAULT_SEED_TEMP_CAP_C)
    if seeded_t is None:
        seeded_t = _FALLBACK_DESIGN_T_C

    # Pressure — curve-interpolated rated P at the seeded T (so the
    # design point sits on the curve). Falls through to hottest_point /
    # rating defaults when no curve is available.
    if seeded_p is None and has_curve and seeded_t is not None:
        seeded_p = wt_calc.interpolate_pressure(temps_curve, pressures_curve, seeded_t)
    if seeded_p is None:
        seeded_p = hottest.get("pressure_barg")
    if seeded_p is None:
        # No P-T curve at all — fall back on the rating's cold-end pressure.
        seeded_p = _rating_fallback_pressure_barg(rating)

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
        # Form schema for Section 2 — drives which inputs the SPA
        # renders. To add/remove/reorder a field, edit
        # `_build_design_conditions_inputs` above; no SPA change needed.
        "design_conditions_inputs": _build_design_conditions_inputs(derived_conditions),
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
