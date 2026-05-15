"""B31.3 wall-thickness engine — Python port of the client-side
calc in src/lib/pmsCalc.ts.

Used by the save flow to snapshot the full per-NPS WT table into the
`payload` JSONB column at save time, so saved PMSes carry every derived
value the SPA would render (instead of relying on the recall-time
client to recompute).

References:
  • ASME B31.3 §304.1.2 Eq. 3a  — internal-pressure wall thickness
  • ASME B31.3 Table A-1        — allowable stress S vs T
  • ASME B31.3 Table 304.1.1    — Y coefficient vs T
  • ASME B31.3 Table 302.3.5    — W weld-strength reduction factor
  • ASME B36.10M / B36.19M      — schedule selection
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Optional

from app.config import settings


# ── Constants ──────────────────────────────────────────────────────

PSI_TO_MPA = 0.00689476
BARG_TO_PSIG = 14.5038
MILL_TOLERANCE = 0.125          # 12.5% — ASME B36.10M seamless
HYDROTEST_FACTOR = 1.5          # ASME B31.3 §345.4.2(a)
OPERATING_FACTOR = 0.8          # rule-of-thumb 80%-of-design estimate


# ── Helpers ────────────────────────────────────────────────────────

def barg_to_psig(b: Optional[float]) -> Optional[float]:
    return None if b is None else b * BARG_TO_PSIG


def c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9 / 5 + 32


def parse_corrosion_mm(ca: Optional[str]) -> float:
    if not ca:
        return 0.0
    if re.match(r"^\s*NIL\s*$", ca, re.I):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", ca)
    return float(m.group(1)) if m else 0.0


def joint_efficiency_from_label(joint_type: Optional[str]) -> float:
    return {
        "Seamless":      1.0,
        "EFW, 100% RT":  1.0,
        "ERW":           0.85,
        "EFW":           0.85,
    }.get((joint_type or "").strip(), 1.0)


def material_uses_stainless_schedules(material: Optional[str]) -> bool:
    if not material:
        return False
    return bool(re.search(
        r"(?:^|\b)(SS\s*316|SS\s*304|TP\s*316|TP\s*304|6\s*MO|N08367)",
        material, re.I,
    ))


# ── Linear interpolation ──────────────────────────────────────────

def _interpolate_at_temp(by_temp_c: dict, target_t: float) -> tuple[Optional[float], Optional[str]]:
    if not by_temp_c:
        return None, None
    keys = sorted(float(k) for k in by_temp_c.keys())
    if not keys:
        return None, None
    if target_t <= keys[0]:
        v = float(by_temp_c[_match_key(by_temp_c, keys[0])])
        return v, ("low" if target_t < keys[0] else None)
    last = keys[-1]
    if target_t >= last:
        v = float(by_temp_c[_match_key(by_temp_c, last)])
        return v, ("high" if target_t > last else None)
    for i in range(len(keys) - 1):
        t1, t2 = keys[i], keys[i + 1]
        if t1 <= target_t <= t2:
            v1 = float(by_temp_c[_match_key(by_temp_c, t1)])
            v2 = float(by_temp_c[_match_key(by_temp_c, t2)])
            frac = (target_t - t1) / (t2 - t1)
            return v1 + frac * (v2 - v1), None
    return float(by_temp_c[_match_key(by_temp_c, last)]), "high"


def _match_key(by_temp_c: dict, target: float) -> str:
    """Find the actual string key in by_temp_c that equals `target`.
    JSON files vary: some use ints ('38'), some use floats ('38.0')."""
    target_int = int(target) if float(target).is_integer() else None
    for k in by_temp_c:
        try:
            if float(k) == target:
                return k
        except (TypeError, ValueError):
            continue
    # Fallback — should not happen if `target` came from the same dict.
    return str(target_int if target_int is not None else target)


def lookup_stress(stress_table: Optional[dict], temp_c: Optional[float]) -> Optional[dict]:
    if not stress_table or temp_c is None:
        return None
    by_temp = stress_table.get("stress_psi_by_temp_c") or {}
    value, clamped = _interpolate_at_temp(by_temp, temp_c)
    if value is None:
        return None
    rounded = round(value / 100) * 100
    return {
        "stress_psi": rounded,
        "stress_mpa": round(rounded * PSI_TO_MPA * 10) / 10,
        "clamped": clamped,
    }


def lookup_y(y_curve: Optional[dict], temp_c: Optional[float]) -> Optional[dict]:
    if not y_curve or temp_c is None:
        return None
    temps = y_curve.get("temperatures_c") or []
    yvals = y_curve.get("y_values") or []
    if not temps or not yvals or len(temps) != len(yvals):
        return None
    by_temp = {str(temps[i]): yvals[i] for i in range(len(temps)) if yvals[i] is not None}
    value, clamped = _interpolate_at_temp(by_temp, temp_c)
    if value is None:
        return None
    return {"y": round(value * 100) / 100, "clamped": clamped}


def interpolate_pressure(temps: list[float], pressures: list[float], target_t: float) -> Optional[float]:
    if not temps or not pressures:
        return None
    if target_t <= temps[0]:
        return float(pressures[0])
    if target_t >= temps[-1]:
        return float(pressures[-1])
    for i in range(len(temps) - 1):
        if temps[i] <= target_t <= temps[i + 1]:
            t1, t2 = temps[i], temps[i + 1]
            p1, p2 = pressures[i], pressures[i + 1]
            return p1 + (p2 - p1) * (target_t - t1) / (t2 - t1)
    return float(pressures[-1])


def interpolate_temperature(temps: list[float], pressures: list[float], target_p: float) -> Optional[float]:
    """Inverse of `interpolate_pressure` — given a target rating pressure,
    return the temperature at which the curve hits that pressure.

    ASME B16.5 P-T curves are monotonically non-increasing in T:
      • target_p ≥ cold-end P → clamp to T[0]   (user-typed P exceeds
        what the class can deliver → cold-end is the relevant point)
      • target_p ≤ hot-end P  → clamp to T[last] (engineer can run all
        the way to the hottest indexed temperature)
      • flat segment          → pick the hot end of that flat region
        (most lenient T that still meets the rating, e.g. for GRE)
    """
    if not temps or not pressures:
        return None
    if target_p >= pressures[0]:
        return float(temps[0])
    if target_p <= pressures[-1]:
        return float(temps[-1])
    for i in range(len(pressures) - 1):
        p1, p2 = pressures[i], pressures[i + 1]
        if p1 >= target_p >= p2:
            if p1 == p2:
                return float(temps[i + 1])
            t1, t2 = temps[i], temps[i + 1]
            return t1 + (t2 - t1) * (p1 - target_p) / (p1 - p2)
    return float(temps[-1])


# ── NPS + pipe-dimension loaders (cached) ──────────────────────────

_GRE_PATTERN = re.compile(r"(?i)\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced")
_BONSTRAND_SVC = re.compile(r"(?i)Hypochlorite|BONSTRAND")
_NPS_OVERRIDES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\bCuNi\b|C70600|B466"),       "nps_dimensions_cuni.json"),
    (re.compile(r"(?i)\bCOPPER\b|C12200|\bB42\b"),  "nps_dimensions_copper.json"),
    (re.compile(r"(?i)\bCPVC\b"),                   "nps_dimensions_cpvc.json"),
    (re.compile(r"(?i)\bTITANIUM\b|\bTi\b|B861"),   "nps_dimensions_titanium.json"),
    (re.compile(r"(?i)Tubing|N08367|6\s*MO"),       "nps_dimensions_tubing.json"),
]


def _resolve_nps_file(material: Optional[str], service: Optional[str]) -> str:
    if not material:
        return "nps_dimensions.json"
    if _GRE_PATTERN.search(material):
        if service and _BONSTRAND_SVC.search(service):
            return "nps_dimensions_gre_bonstrand.json"
        return "nps_dimensions_gre.json"
    for pat, fname in _NPS_OVERRIDES:
        if pat.search(material):
            return fname
    return "nps_dimensions.json"


@lru_cache(maxsize=16)
def _load_json(filename: str) -> dict:
    path = settings.data_dir / filename
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_nps_dimensions(material: Optional[str], service: Optional[str]) -> dict:
    return _load_json(_resolve_nps_file(material, service))


@lru_cache(maxsize=1)
def _b3610_indexed() -> dict[float, list[dict]]:
    data = _load_json("pipe_dimensions_b3610.json")
    by_nps: dict[float, list[dict]] = {}
    for r in data.get("rows", []):
        if r.get("schedule") is None and r.get("identification") is None:
            continue
        if r.get("wt_mm") is None:
            continue
        by_nps.setdefault(r["nps_decimal"], []).append(r)
    for k in by_nps:
        by_nps[k].sort(key=lambda r: r["wt_mm"])
    return by_nps


@lru_cache(maxsize=1)
def _b3619_indexed() -> dict[float, list[dict]]:
    try:
        data = _load_json("pipe_dimensions_b3619.json")
    except FileNotFoundError:
        return {}
    by_nps: dict[float, list[dict]] = {}
    for r in data.get("rows", []):
        if r.get("wt_mm") is None:
            continue
        by_nps.setdefault(r["nps_decimal"], []).append(r)
    for k in by_nps:
        by_nps[k].sort(key=lambda r: r["wt_mm"])
    return by_nps


# ── Schedule pick ─────────────────────────────────────────────────

def pick_schedule(
    nps_decimal: float, calc_thk_mm: Optional[float], material: str,
    nps_row: Optional[dict] = None,
) -> Optional[dict]:
    """Pick lightest pipe wall ≥ calc_thk for the given NPS, honouring
    per-material overrides (Titanium A70 etc.) when the NPS row carries
    explicit (sch, wt_mm) fields."""
    if nps_row and nps_row.get("sch") is not None and nps_row.get("wt_mm") is not None:
        return {
            "sch_display": str(nps_row["sch"]),
            "wt_mm":       float(nps_row["wt_mm"]),
            "status":      "OK",
            "table":       "project-spec",
        }
    if calc_thk_mm is None:
        return None

    use_ss = material_uses_stainless_schedules(material)
    primary = _b3619_indexed() if use_ss else _b3610_indexed()
    fallback = _b3610_indexed() if use_ss else None

    def _from(table: dict) -> Optional[dict]:
        rows = table.get(nps_decimal) or []
        for r in rows:
            if (r.get("wt_mm") or 0) >= calc_thk_mm:
                return r
        return None

    pick = _from(primary)
    used = "B36.19M" if use_ss else "B36.10M"
    if not pick and fallback:
        pick = _from(fallback)
        if pick:
            used = "B36.10M (fallback from B36.19M)"

    status = "OK"
    if not pick:
        # Couldn't satisfy calc_thk — report heaviest row + NOT OK.
        heavy = fallback or primary
        rows = heavy.get(nps_decimal) or []
        if not rows:
            return None
        pick = rows[-1]
        status = "NOT OK"

    display = pick.get("schedule")
    if display is None:
        display = pick.get("identification") or "—"
    return {
        "sch_display": str(display),
        "wt_mm":       float(pick.get("wt_mm") or 0),
        "status":      status,
        "table":       used,
    }


# ── Eq. 3a per-NPS ────────────────────────────────────────────────

def _t_d_ratio(P_psi, S_psi, E, Y, W) -> Optional[float]:
    if P_psi is None or S_psi is None or Y is None or W is None:
        return None
    denom = 2 * (S_psi * E * W + P_psi * Y)
    return (P_psi / denom) if denom > 0 else None


def compute_wall_thickness_rows(
    *,
    resolved: dict,
    material: str,
    ca: str,
    design_pressure_barg: Optional[float],
    design_temp_c: Optional[float],
    joint_type: Optional[str],
    service: Optional[str] = None,
    use_design_point_only: bool = False,
) -> list[dict]:
    """B31.3 §304.1.2 Eq. 3a per NPS.

    `use_design_point_only` controls the worst-case logic:
      • False (default — "envelope" mode): evaluate Eq. 3a at BOTH the
        cold-end of the P-T curve (Min T / Max P) AND at the user-
        supplied design point (Max T 300 °C cap / rated P at that T),
        then take the max. This is the conservative default that
        designs for the class's full operating envelope.
      • True ("user-pinned" mode): evaluate Eq. 3a at the design point
        only. Used when the engineer has explicitly typed a specific
        P / T pair via the chat agent or the design inputs — the
        cold-end is intentionally skipped because the user is
        committing to that single operating point.

    Returns one row per NPS in the material's dimension list."""
    nps_doc = load_nps_dimensions(material, service)
    nps_rows = nps_doc.get("rows") or []
    if not nps_rows:
        return []

    cf = resolved.get("code_factors") or {}
    stress_table = cf.get("stress_table")
    y_curve = cf.get("y_curve")
    pt = resolved.get("pressure_temperature") or {}

    # Design point (always evaluated).
    P_psi_d = barg_to_psig(design_pressure_barg) if design_pressure_barg is not None else None
    s_hot = lookup_stress(stress_table, design_temp_c) if (stress_table and design_temp_c is not None) else None
    S_d = s_hot["stress_psi"] if s_hot else None

    # Cold-end point (only used in envelope mode).
    P_psi_c = None
    S_c = None
    if not use_design_point_only:
        cold_pbarg = (pt.get("cold_point") or {}).get("pressure_barg")
        cold_tc = (pt.get("temperatures_c") or [None])[0]
        P_psi_c = barg_to_psig(cold_pbarg)
        s_cold = lookup_stress(stress_table, cold_tc) if (stress_table and cold_tc is not None) else None
        S_c = s_cold["stress_psi"] if s_cold else None

    E = joint_efficiency_from_label(joint_type)
    y_at = lookup_y(y_curve, design_temp_c) if design_temp_c is not None else None
    Y = y_at["y"] if y_at else None
    W = 1.0 if (design_temp_c is not None and design_temp_c <= 510) else None

    C_mm = parse_corrosion_mm(ca)
    mill_tol = MILL_TOLERANCE

    tD_design = _t_d_ratio(P_psi_d, S_d, E, Y, W)
    if use_design_point_only:
        tD = tD_design
    else:
        tD_cold = _t_d_ratio(P_psi_c, S_c, E, Y, W)
        candidates = [v for v in (tD_cold, tD_design) if v is not None]
        tD = max(candidates) if candidates else None
    # MAWP is always computed at the operating point (design T).
    S = S_d

    out: list[dict] = []
    for r in nps_rows:
        D = float(r["od_mm"])
        t_mm = (tD * D) if tD is not None else None
        d_over_6 = D / 6
        validity = (
            "OK" if (t_mm is not None and t_mm < d_over_6)
            else ("ALERT" if t_mm is not None else None)
        )
        tm = (t_mm + C_mm) if t_mm is not None else None
        calc_thk = (tm / (1 - mill_tol)) if tm is not None else None
        sched = pick_schedule(float(r["nps_decimal"]), calc_thk, material, r)
        sel_thk_mm = sched["wt_mm"] if sched else None

        # MAWP @ design T using inverse Eq. 3a, t_eff = sel*(1-mill) − c
        mawp_barg: Optional[float] = None
        margin_pct: Optional[float] = None
        if sel_thk_mm is not None and S is not None and W is not None and Y is not None:
            t_eff_mm = sel_thk_mm * (1 - mill_tol) - C_mm
            if t_eff_mm > 0:
                t_eff_in = t_eff_mm / 25.4
                D_in = D / 25.4
                denom = D_in - 2 * Y * t_eff_in
                if denom > 0:
                    mawp_psi = (2 * S * E * W * t_eff_in) / denom
                    mawp_barg = mawp_psi / BARG_TO_PSIG
                    if design_pressure_barg and design_pressure_barg > 0:
                        margin_pct = ((mawp_barg - design_pressure_barg) / design_pressure_barg) * 100

        # SEL. THK column — backend owns the decimal precision so every
        # client (page UI, SPA, Excel) renders the same value. Project
        # convention: 1 decimal place for selected wall thickness. The
        # NOT-OK fallback (which echoes calc_thk_mm in this column)
        # uses the same precision so the column reads consistently.
        # Numeric `sel_thk_mm` / `calc_thk_mm` stay full-precision for
        # downstream calcs and the Excel exporter.
        _SEL_THK_DECIMALS = 1
        sel_thk_mm_display = (
            f"{sel_thk_mm:.{_SEL_THK_DECIMALS}f}"
            if sel_thk_mm is not None else None
        )
        calc_thk_mm_display = (
            f"{calc_thk:.{_SEL_THK_DECIMALS}f}"
            if calc_thk is not None else None
        )

        out.append({
            "nps":                 str(r["nps"]),
            "nps_decimal":         float(r["nps_decimal"]),
            "od_mm":               D,
            "t_mm":                t_mm,
            "d_over_6":            d_over_6,
            "validity":            validity,
            "tm_mm":               tm,
            "mill_tol":            mill_tol,
            "calc_thk_mm":         calc_thk,
            "calc_thk_mm_display": calc_thk_mm_display,
            "sch_display":         sched["sch_display"] if sched else None,
            "sel_thk_mm":          sel_thk_mm,
            "sel_thk_mm_display":  sel_thk_mm_display,
            "sch_status":          sched["status"] if sched else None,
            "mawp_barg":           mawp_barg,
            "margin_pct":          margin_pct,
        })
    return out


def summary_stats(rows: list[dict], pt: Optional[dict], design_pressure_barg: Optional[float]) -> dict:
    mawps = [r["mawp_barg"] for r in rows if r.get("mawp_barg") is not None]
    margins = [r["margin_pct"] for r in rows if r.get("margin_pct") is not None]
    max_rated = None
    if pt and pt.get("pressures_barg"):
        max_rated = max(pt["pressures_barg"])
    elif design_pressure_barg is not None:
        max_rated = design_pressure_barg
    hydro = (max_rated * HYDROTEST_FACTOR) if max_rated is not None else None
    return {
        "min_mawp_barg":     min(mawps) if mawps else None,
        "max_mawp_barg":     max(mawps) if mawps else None,
        "min_margin_pct":    min(margins) if margins else None,
        "hydrotest_barg":    hydro,
        "total_nps_sizes":   len(rows),
        # Project-policy constants — surfaced here so the frontend
        # never has to hardcode them. ASME B36.10M seamless mill
        # tolerance and B31.3 §345 hydrotest factor.
        "mill_tolerance":     MILL_TOLERANCE,
        "hydrotest_factor":   HYDROTEST_FACTOR,
        "operating_factor":   OPERATING_FACTOR,
    }


# ── Engineering Requirements & Flags ──────────────────────────────

def evaluate_flags(
    *, resolved: dict, material: str, design_temp_c: Optional[float], mdmt_c: Optional[float],
) -> list[dict]:
    """Faithful port of evaluateFlags() in pmsCalc.ts."""
    flags: list[dict] = []
    cf = resolved.get("code_factors") or {}
    clean_mat = re.sub(r"\s*\(.*?\)\s*", "", material or "").strip()
    is_austenitic = (cf.get("y_curve") or {}).get("category") == "austenitic_steels"

    if not cf.get("stress_table"):
        flags.append({
            "level": "critical",
            "title": "No ASME B31.3 Allowable Stress",
            "body":  f"Material {clean_mat} is not tabulated in B31.3 Table A-1 — likely composite or non-metal pipe (e.g. GRE per ISO 14692, CPVC per ASTM F441). Wall thickness cannot be computed from Eq. 3a; refer to material-specific design rules.",
        })
    if material and re.search(r"NACE", material, re.I):
        flags.append({
            "level": "mandatory",
            "title": "NACE MR0175 / ISO 15156 — Sour Service",
            "body":  "Material hardness controlled per NACE MR0175 (HRC ≤ 22 base metal, HV ≤ 250 weld). Heat-treatment certification, HIC / SSC qualification required. Project policy caps service temperature at 250°C for sour-service lines.",
        })
    if material and re.match(r"^LTCS", material, re.I):
        md = f"{mdmt_c:.0f}°C" if (mdmt_c is not None) else "MDMT"
        flags.append({
            "level": "mandatory",
            "title": "Low-Temperature Service — A333 Gr 6",
            "body":  f"Charpy V-notch impact testing per ASTM A333 Gr 6 — minimum 13.5 ft·lbf at {md}. Welding procedure qualification at MDMT is mandatory; PWHT records to be retained.",
        })
    if (cf.get("stress_table") or {}).get("key") == "API5LX60":
        flags.append({
            "level": "note",
            "title": "API 5L X60 PSL-2 Promoted (High-Pressure NACE)",
            "body":  "For 1500# / 2500# CS NACE classes, project convention specs API 5L Grade X60 PSL-2 line pipe (S = 25,000 psi cold) instead of A106 Gr B (S = 20,000 psi). Wall thickness uses the X60 stress curve.",
        })
    if design_temp_c is not None and design_temp_c > 482 and not is_austenitic:
        flags.append({
            "level": "warning",
            "title": "Y Coefficient — Above Ferritic Baseline (482°C)",
            "body":  f"Design temperature {design_temp_c:.0f}°C exceeds the 482°C ferritic baseline in B31.3 Table 304.1.1. Y rises in steps (0.4 → 0.5 at 510°C → 0.7 at 538°C). Verify the interpolation against your actual material spec.",
        })
    if design_temp_c is not None and design_temp_c > 510:
        flags.append({
            "level": "warning",
            "title": "W Factor — Above Creep Onset (510°C)",
            "body":  f"Design temperature {design_temp_c:.0f}°C exceeds the 510°C creep onset. W = 1 still applies for seamless / 100% RT welded pipe; for lower joint efficiencies W < 1 per Table 302.3.5 — review weld strength reduction.",
        })

    # Customized (new-spec) PMS — fires when design T exceeds the
    # standard envelope cap (300 °C). The class_code in the snapshot
    # gets a "New-spec-[base]" rename in this zone; this flag makes
    # the engineer-review requirement visible inside the report's
    # Engineering Requirements & Flags panel.
    if design_temp_c is not None and design_temp_c > 300:
        flags.append({
            "level": "mandatory",
            "title": "Customized PMS — Engineering Review Required",
            "body":  (
                f"Design temperature {design_temp_c:.0f}°C is outside the "
                "standard 0–300 °C envelope. This PMS has been generated "
                "as a non-standard variant (class code prefixed with "
                "\"New-spec-\") and the wall-thickness calc uses your "
                "single design point only (no cold-end envelope check). "
                "Please consult a technical engineer to verify suitability "
                "for sustained service before issuing this report."
            ),
        })

    # Carbon-steel creep / graphitization range. ASME B16.5 Note (1) for
    # Group 1.1 caps prolonged use of plain CS at 425 °C; B31.3 starts
    # creep checks ~370 °C for CS. These two flags fire in the 370–425
    # range (caution) and >425 °C (stronger warning) — exactly the
    # zone an engineer needs flagged when running CS hot.
    is_plain_cs = bool(material) and bool(re.search(r"^\s*(?:CS|LTCS)\s*(?:NACE)?\s*$", material, re.I))
    if is_plain_cs and design_temp_c is not None and 370 < design_temp_c <= 425:
        flags.append({
            "level": "warning",
            "title": "Carbon Steel — Creep & Graphitization Caution (370–425 °C)",
            "body":  f"Design temperature {design_temp_c:.0f}°C enters the creep range for plain carbon steel (onset ~370 °C). Long-term exposure can cause graphitization of the carbide phase. Consider Cr-Mo alloy steel (e.g. P11 / P22) if continuous service is expected.",
        })
    if is_plain_cs and design_temp_c is not None and design_temp_c > 425:
        flags.append({
            "level": "warning",
            "title": "Carbon Steel — Above ASME B16.5 Prolonged-Service Limit (425 °C)",
            "body":  f"Design temperature {design_temp_c:.0f}°C exceeds 425 °C — per ASME B16.5 Table 2-1.1 Note (1), prolonged use of Group 1.1 carbon steel above 425 °C is permissible but NOT recommended (carbide may convert to graphite). Specify a chromium-molybdenum alloy steel for continuous service.",
        })
    if material and re.search(r"GALV", material, re.I) and design_temp_c is not None and design_temp_c > 200:
        flags.append({
            "level": "warning",
            "title": "Galvanizing — Above Coating Temperature Limit",
            "body":  f"Design temperature {design_temp_c:.0f}°C exceeds the typical hot-dip galvanized zinc coating limit (~200°C). Coating may degrade in service — verify temperature compatibility or specify an alternative coating system.",
        })
    if cf.get("stress_table"):
        flags.append({
            "level": "note",
            "title": "Operating Conditions — 80% Estimate",
            "body":  "The “Operating (est. 80%)” pressure / temperature on the Derived Design Conditions card is a rule-of-thumb estimate (0.8 × design). Replace with actual process operating point when available.",
        })
    return flags


# ── Materials tab snapshot ─────────────────────────────────────────

_COMPONENT_ROWS: list[dict] = [
    {"name": "90° LR Elbow",        "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "45° Elbow",           "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "Equal Tee",           "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "Reducing Tee",        "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "Concentric Reducer",  "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "Eccentric Reducer",   "spec_key": "fittings",      "sch_kind": "pipe",  "standard": "ASME B 16.9"},
    {"name": "Pipe Cap",            "spec_key": "fittings",      "sch_kind": "b16.9", "standard": "ASME B 16.9"},
    {"name": "Plug",                "spec_key": "fittings",      "sch_kind": "—",     "standard": "Hex Head Plug, ASME B 16.11"},
    {"name": "Weldolet",            "spec_key": "branch_outlet", "sch_kind": "—",     "standard": "MSS SP-97"},
]


def _dominant_schedule_in_range(rows: list[dict], lo: float, hi: float) -> Optional[str]:
    counts: dict[str, int] = {}
    for r in rows:
        try:
            n = float(r["nps"])
        except (TypeError, ValueError):
            continue
        if n < lo or n > hi:
            continue
        sch = r.get("sch_display")
        if not sch or r.get("sch_status") == "NOT OK":
            continue
        counts[sch] = counts.get(sch, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _format_schedule(s: Optional[str]) -> str:
    if not s:
        return "—"
    return s if re.match(r"^[A-Z]{2,}$", s) else f"SCH {s}"


def _connection_wording(family: Optional[str], is_large_bore: bool) -> str:
    if not family:
        return "—"
    if is_large_bore:
        return "Butt Weld (SCH to match pipe), Welded"
    if re.search(r"COPPER", family, re.I):
        return "Sweat / threaded fitting per B16.22"
    if re.search(r"GRE", family, re.I):
        return "Adhesive bonded / threaded per ISO 14692"
    if re.search(r"CPVC", family, re.I):
        return "Solvent weld per ASTM F 493"
    if re.search(r"TUBING", family, re.I):
        return "Compression / cone & thread (Swagelok)"
    return "Butt Weld (SCH to match pipe), Seamless"


def _fitting_schedule_cell(kind: str, bore_sch: Optional[str]) -> str:
    if kind == "—" or not bore_sch:
        return "—"
    if kind == "b16.9":
        return "ASME B 16.9"
    return f"Sch {bore_sch}"


def _build_bore_rows(specs: dict, bore_sch: Optional[str]) -> list[dict]:
    rows = [{
        "component": "Pipe",
        "material":  specs.get("pipe") or "—",
        "schedule":  _format_schedule(bore_sch),
        "standard":  "ASTM",
    }]
    for c in _COMPONENT_ROWS:
        rows.append({
            "component": c["name"],
            "material":  specs.get(c["spec_key"]) or "—",
            "schedule":  _fitting_schedule_cell(c["sch_kind"], bore_sch),
            "standard":  c["standard"],
        })
    return rows


def build_materials_snapshot(resolved: dict, wt_rows: list[dict]) -> Optional[dict]:
    specs = (resolved.get("code_factors") or {}).get("fitting_specs") or {}
    if not specs:
        return None
    small_sch = _dominant_schedule_in_range(wt_rows, 0, 2)
    large_sch = _dominant_schedule_in_range(wt_rows, 2.5, 80)
    family = specs.get("family")
    return {
        "small_bore": {
            "range":       'NPS ½" – 2"',
            "connection":  _connection_wording(family, False),
            "schedule":    _format_schedule(small_sch),
            "rows":        _build_bore_rows(specs, small_sch),
        },
        "large_bore": {
            "range":       'NPS 2½" – 36"',
            "connection":  _connection_wording(family, True),
            "schedule":    _format_schedule(large_sch),
            "rows":        _build_bore_rows(specs, large_sch),
        },
    }


# ── Adequacy banner + derived design conditions ───────────────────

def adequacy(pt: Optional[dict], design_pressure_barg: Optional[float], design_temp_c: Optional[float]) -> Optional[dict]:
    """Returns {adequate, rated_p_at_design_t} or None when the check
    can't be made (missing data)."""
    if not pt or design_pressure_barg is None or design_temp_c is None:
        return None
    temps = pt.get("temperatures_c") or []
    pressures = pt.get("pressures_barg") or []
    if not temps or not pressures:
        return None
    rated = interpolate_pressure(temps, pressures, design_temp_c)
    if rated is None:
        return None
    return {
        "rated_pressure_at_design_t_barg": rated,
        "design_pressure_barg":            design_pressure_barg,
        "design_temp_c":                   design_temp_c,
        # Same 0.05 barg tolerance the SPA banner uses.
        "adequate": rated + 0.05 >= design_pressure_barg,
    }


def build_formula_example(
    *,
    resolved: dict,
    material: str,
    ca: str,
    design_pressure_barg: Optional[float],
    design_temp_c: Optional[float],
    joint_type: Optional[str],
    service: Optional[str] = None,
    use_design_point_only: bool = False,
) -> Optional[dict]:
    """Worked example for the B31.3 §304.1.2 Eq. 3a formula card on
    Tab 2. Picks NPS 6 (project convention) or the largest available
    NPS as a fallback. Returns None when the calc can't be performed
    (no stress table for the material, etc.) — the frontend then
    renders a "worked example unavailable" message.

    Mirrors the renderFormulaCard() routine in the legacy TS client
    so the same numbers appear in the new compute endpoint."""
    nps_doc = load_nps_dimensions(material, service)
    nps_rows = nps_doc.get("rows") or []
    if not nps_rows:
        return None

    PREFERRED = [6.0, 4.0, 3.0, 2.0]
    example = None
    for n in PREFERRED:
        example = next((r for r in nps_rows if float(r.get("nps_decimal", -1)) == n), None)
        if example:
            break
    if example is None:
        example = max(nps_rows, key=lambda r: float(r.get("nps_decimal", -1)))

    D_mm = float(example["od_mm"])
    D_in = D_mm / 25.4

    cf = resolved.get("code_factors") or {}
    stress_table = cf.get("stress_table")
    y_curve = cf.get("y_curve")
    pt = resolved.get("pressure_temperature") or {}

    # Design point (always evaluated).
    P2_psi = barg_to_psig(design_pressure_barg) if design_pressure_barg is not None else None
    s_hot = lookup_stress(stress_table, design_temp_c) if (stress_table and design_temp_c is not None) else None
    S2 = s_hot["stress_psi"] if s_hot else None

    # Cold-end point (only used in envelope mode).
    P1_psi = None
    S1 = None
    cold_label = None
    if not use_design_point_only:
        cold_pbarg = (pt.get("cold_point") or {}).get("pressure_barg")
        cold_tc = (pt.get("temperatures_c") or [None])[0]
        cold_label = (pt.get("temp_labels") or [None])[0] or (
            str(int(cold_tc)) if cold_tc is not None else "—"
        )
        P1_psi = barg_to_psig(cold_pbarg)
        s_cold = lookup_stress(stress_table, cold_tc) if (stress_table and cold_tc is not None) else None
        S1 = s_cold["stress_psi"] if s_cold else None

    E = joint_efficiency_from_label(joint_type)
    y_at = lookup_y(y_curve, design_temp_c) if design_temp_c is not None else None
    Y = y_at["y"] if y_at else 0.4
    Y_label = (y_curve or {}).get("label", "unknown")
    W = 1.0 if (design_temp_c is not None and design_temp_c <= 510) else None

    C_mm = parse_corrosion_mm(ca)
    C_in = C_mm / 25.4
    mill_tol = MILL_TOLERANCE

    # Eq. 3a per case in inches: t = P·D / [2·(S·E·W + P·Y)]
    t1_in = None
    if (not use_design_point_only) and P1_psi is not None and S1 is not None and W is not None:
        t1_in = (P1_psi * D_in) / (2 * (S1 * E * W + P1_psi * Y))
    t2_in = None
    if P2_psi is not None and S2 is not None and W is not None:
        t2_in = (P2_psi * D_in) / (2 * (S2 * E * W + P2_psi * Y))

    candidates = [v for v in (t1_in, t2_in) if v is not None]
    if not candidates:
        return {
            "available": False,
            "nps":        str(example["nps"]),
            "od_in":      D_in,
            "od_mm":      D_mm,
            "E": E, "W": W, "Y": Y, "Y_label": Y_label,
            "C_mm": C_mm, "C_in": C_in,
            "mill_tolerance": mill_tol,
            "reason": "Allowable stress unavailable for this material.",
        }
    # Decide which case governs (when both present, max wins).
    case1_governs = (t1_in is not None) and (t2_in is None or t1_in >= t2_in)
    t_press_in = t1_in if case1_governs else t2_in
    tm_in = t_press_in + C_in
    T_in = tm_in / (1 - mill_tol)
    T_mm = T_in * 25.4

    return {
        "available": True,
        "nps":        str(example["nps"]),
        "od_in":      D_in,
        "od_mm":      D_mm,
        "E": E, "W": W, "Y": Y, "Y_label": Y_label,
        "C_mm": C_mm, "C_in": C_in,
        "mill_tolerance": mill_tol,
        # In design-point-only mode, case_1 is None — the SPA's
        # `{formula.case_1 && (...)}` guard hides the cold-end row.
        "case_1": None if use_design_point_only else {
            "label":      f"Min T / Max P @ {cold_label}",
            "P_psig":     P1_psi,
            "S_psi":      S1,
            "t_press_in": t1_in,
            "governs":    case1_governs,
        },
        "case_2": {
            "label":      f"Design Point @ {design_temp_c}°C" if design_temp_c is not None else "Design Point",
            "P_psig":     P2_psi,
            "S_psi":      S2,
            "t_press_in": t2_in,
            "governs":    not case1_governs and t2_in is not None,
        },
        "governing_case":   1 if case1_governs else 2,
        "t_press_in":       t_press_in,
        "tm_in":            tm_in,
        "T_req_in":         T_in,
        "T_req_mm":         T_mm,
    }


def derived_design_conditions(
    *, pt: Optional[dict], design_pressure_barg: Optional[float],
    design_temp_c: Optional[float], mdmt_c: Optional[float],
) -> dict:
    p_psig  = barg_to_psig(design_pressure_barg)
    op_p    = (design_pressure_barg * OPERATING_FACTOR) if design_pressure_barg is not None else None
    op_t    = (design_temp_c * OPERATING_FACTOR) if design_temp_c is not None else None
    # Hydrotest = max rated P × 1.5 (preferring the indexed cold-point)
    max_rated = None
    if pt and pt.get("pressures_barg"):
        max_rated = max(pt["pressures_barg"])
    elif design_pressure_barg is not None:
        max_rated = design_pressure_barg
    hydro_p_barg = (max_rated * HYDROTEST_FACTOR) if max_rated is not None else None
    return {
        "pressure": {
            "design_barg":           design_pressure_barg,
            "design_psig":           p_psig,
            "hydrotest_barg":        hydro_p_barg,
            "hydrotest_psig":        barg_to_psig(hydro_p_barg),
            "operating_estimate_barg": op_p,
            "operating_estimate_psig": barg_to_psig(op_p),
        },
        "temperature": {
            "design_c":              design_temp_c,
            "design_f":              c_to_f(design_temp_c),
            "operating_estimate_c":  op_t,
            "operating_estimate_f":  c_to_f(op_t),
            "mdmt_c":                mdmt_c,
            "mdmt_f":                c_to_f(mdmt_c),
        },
    }
