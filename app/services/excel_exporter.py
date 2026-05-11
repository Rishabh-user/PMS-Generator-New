"""Excel export for a resolved PMS class.

Generates a single-sheet datasheet matching the look of the project's
Excel templates — Identification header, P-T Rating table, Wall Thickness
Calculation table, Pipe & Fittings tables (Small + Large bore), Flange /
Bolts / Gasket / Spectacle / Valves cards, AI engineering notes if any.

Backed by openpyxl (already a dependency of the extractors). The exporter
re-uses the resolver + lookup services so values match the live UI byte-
for-byte — no second source of truth.

Public entry point:
    build_workbook(class_code, rating, material, ca, service,
                   design_p_barg, design_t_c, mdmt_c, joint_type) -> BytesIO
"""
from __future__ import annotations

import io
import json
import math
import re
from datetime import datetime
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import settings
from app.services import (
    class_resolver,
    fitting_specs,
    flange_specs,
    pt_lookup,
    stress_lookup,
    y_lookup,
)


# ──────────────────────────────────────────────────────────────────────
# Styles
# ──────────────────────────────────────────────────────────────────────
NAVY        = "FF0E3A5C"
NAVY_LIGHT  = "FFF0F6FC"
HEADER_TEXT = "FFFFFFFF"
GRAY_50     = "FFF8FAFC"
GRAY_100    = "FFF1F5F9"
AMBER_BG    = "FFFFFBEB"
GREEN_BG    = "FFF0FDF4"
RED_BG      = "FFFEF2F2"

_thin = Side(style="thin",   color="FFE2E8F0")
_med  = Side(style="medium", color="FF0E3A5C")

BORDER_ALL  = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
BORDER_HEAD = Border(left=_med,  right=_med,  top=_med,  bottom=_med)

FONT_TITLE      = Font(name="Calibri", size=14, bold=True, color=NAVY[2:])
FONT_SECTION    = Font(name="Calibri", size=11, bold=True, color=NAVY[2:])
FONT_HEADER     = Font(name="Calibri", size=10, bold=True, color="FFFFFFFF")
FONT_LABEL      = Font(name="Calibri", size=10, bold=True)
FONT_VALUE      = Font(name="Calibri", size=10)
FONT_VALUE_BOLD = Font(name="Calibri", size=10, bold=True)
FONT_NOTE       = Font(name="Calibri", size=9, italic=True, color="FF64748B")

FILL_HEADER  = PatternFill("solid", fgColor=NAVY)
FILL_BAND    = PatternFill("solid", fgColor=NAVY_LIGHT)
FILL_GRAY    = PatternFill("solid", fgColor=GRAY_50)
FILL_AMBER   = PatternFill("solid", fgColor=AMBER_BG)
FILL_GREEN   = PatternFill("solid", fgColor=GREEN_BG)
FILL_RED     = PatternFill("solid", fgColor=RED_BG)

CENTER       = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT         = Alignment(horizontal="left",   vertical="center", wrap_text=True)
RIGHT        = Alignment(horizontal="right",  vertical="center", wrap_text=True)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _bargToPsig(b: float) -> float:
    return b * 14.5038


def _cToF(c: float) -> float:
    return c * 9 / 5 + 32


def _parse_ca_mm(ca: str) -> float:
    if not ca:
        return 0.0
    if re.match(r"^\s*NIL\s*$", ca, re.I):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", ca)
    return float(m.group(1)) if m else 0.0


def _interp(by_temp_c: dict, target_c: float) -> Optional[float]:
    """Linear interp matching the JS lookupStress / lookupY behaviour."""
    if not by_temp_c:
        return None
    keys = sorted(int(k) for k in by_temp_c.keys())
    if target_c <= keys[0]:
        return float(by_temp_c[str(keys[0])])
    if target_c >= keys[-1]:
        return float(by_temp_c[str(keys[-1])])
    for i in range(len(keys) - 1):
        t1, t2 = keys[i], keys[i + 1]
        if t1 <= target_c <= t2:
            v1, v2 = float(by_temp_c[str(t1)]), float(by_temp_c[str(t2)])
            frac = (target_c - t1) / (t2 - t1)
            return v1 + frac * (v2 - v1)
    return float(by_temp_c[str(keys[-1])])


def _load_json(name: str) -> dict:
    return json.loads((settings.data_dir / name).read_text(encoding="utf-8"))


# NPS list — material-aware and (for GRE) service-aware.
def _nps_rows(material: Optional[str] = None, service: Optional[str] = None) -> list[dict]:
    fname = "nps_dimensions.json"
    if material:
        if re.search(r"\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced", material, re.I):
            fname = (
                "nps_dimensions_gre_bonstrand.json"
                if service and re.search(r"Hypochlorite|BONSTRAND", service, re.I)
                else "nps_dimensions_gre.json"
            )
        elif re.search(r"CuNi|C70600|B466", material, re.I):
            fname = "nps_dimensions_cuni.json"
        elif re.search(r"\bCOPPER\b|C12200|\bB42\b", material, re.I):
            fname = "nps_dimensions_copper.json"
        elif re.search(r"\bCPVC\b", material, re.I):
            fname = "nps_dimensions_cpvc.json"
        elif re.search(r"\bTITANIUM\b|\bTi\b|B861", material, re.I):
            fname = "nps_dimensions_titanium.json"
        elif re.search(r"Tubing|N08367|6\s*MO", material, re.I):
            fname = "nps_dimensions_tubing.json"
    return _load_json(fname)["rows"]


def _b3610_rows() -> dict[float, list[dict]]:
    data = _load_json("pipe_dimensions_b3610.json")
    by_nps: dict[float, list[dict]] = {}
    for r in data.get("rows", []):
        if r.get("schedule") is None and r.get("identification") is None:
            continue   # skip line-pipe-only intermediate WT rows
        by_nps.setdefault(r["nps_decimal"], []).append(r)
    for k in by_nps:
        by_nps[k].sort(key=lambda r: r["wt_mm"])
    return by_nps


def _b3619_rows() -> dict[float, list[dict]]:
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


def _uses_stainless(material: str) -> bool:
    return bool(material) and bool(re.search(
        r"(?:^|\b)(SS\s*316|SS\s*304|TP\s*316|TP\s*304|6\s*MO|N08367)",
        material, re.I,
    ))


def _pick_schedule(
    primary_by_nps: dict,
    nps: float,
    calc_thk_mm: float,
    fallback_by_nps: Optional[dict] = None,
) -> Optional[dict]:
    """Pick the lightest schedule whose WT ≥ calc_thk_mm.

    Stainless service uses B36.19M (5S/10S/40S/80S) primarily. That table
    tops out at 80S — high-pressure stainless classes (e.g. G10 SS316L
    2500#) can need a heavier wall than 80S provides. In that case fall
    back to B36.10M (SCH 160 / XXS) — same OD, just a heavier wall the
    stainless table doesn't list. Without this fallback the picker would
    silently land on 80S, a wall THINNER than required.
    """
    if calc_thk_mm is None:
        return None

    def _scan(by_nps):
        rows = by_nps.get(nps) or [] if by_nps else []
        if not rows:
            return None
        return next((r for r in rows if r["wt_mm"] >= calc_thk_mm), None)

    pick = _scan(primary_by_nps)
    used_fallback = False
    if pick is None and fallback_by_nps:
        pick = _scan(fallback_by_nps)
        if pick is not None:
            used_fallback = True

    status = "OK"
    if pick is None:
        # No wall in either table covers the calc — flag NOT OK and return
        # the heaviest available from the heavier table (fallback if any,
        # else primary). For stainless this surfaces "NOT OK at XXS 7.82"
        # instead of misleading "NOT OK at 80S 3.91".
        heavy_table = fallback_by_nps if fallback_by_nps else primary_by_nps
        rows = (heavy_table.get(nps) if heavy_table else None) or []
        if not rows:
            return None
        pick = rows[-1]
        status = "NOT OK"

    sch = pick.get("schedule") if pick.get("schedule") is not None else (pick.get("identification") or "—")
    return {"sch": str(sch), "wt_mm": pick["wt_mm"], "status": status, "fallback": used_fallback}


def _joint_eff(joint: str) -> float:
    return {
        "Seamless": 1.0, "EFW, 100% RT": 1.0, "ERW": 0.85, "EFW": 0.85,
    }.get(joint or "Seamless", 1.0)


# ──────────────────────────────────────────────────────────────────────
# Sheet builders — each appends rows to ws and returns the next row idx
# ──────────────────────────────────────────────────────────────────────

def _write_kv(ws, row: int, label: str, value: Any, *, bold: bool = False, span: int = 1) -> int:
    """Two-cell key/value row. label in col A, value spanning B..(B+span-1)."""
    ws.cell(row=row, column=1, value=label).font = FONT_LABEL
    ws.cell(row=row, column=1).fill = FILL_GRAY
    ws.cell(row=row, column=1).alignment = LEFT
    ws.cell(row=row, column=1).border = BORDER_ALL
    if span > 1:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=1 + span)
    cell = ws.cell(row=row, column=2, value=value if value is not None else "—")
    cell.font = FONT_VALUE_BOLD if bold else FONT_VALUE
    cell.alignment = LEFT
    cell.border = BORDER_ALL
    return row + 1


def _section_header(ws, row: int, text: str, total_cols: int = 7) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
    c.alignment = CENTER
    c.fill = FILL_HEADER
    c.border = BORDER_HEAD
    return row + 1


def _build_identification(ws, row: int, ctx: dict) -> int:
    """Top header block — PMS code, rating, material, design conditions."""
    # Title
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=7)
    t = ws.cell(row=row, column=1, value="PIPING MATERIAL SPECIFICATION")
    t.font = FONT_TITLE
    t.alignment = CENTER
    t.fill = FILL_BAND
    ws.row_dimensions[row].height = 26
    row += 1

    # Sub-line
    sub = f"PMS Class: {ctx['class_code']}   ·   Generated: {datetime.now():%Y-%m-%d %H:%M}"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=7)
    s = ws.cell(row=row, column=1, value=sub)
    s.font = FONT_NOTE
    s.alignment = CENTER
    s.fill = FILL_BAND
    row += 1

    row += 1  # blank spacer

    row = _section_header(ws, row, "1. IDENTIFICATION")
    row = _write_kv(ws, row, "PMS Code",           ctx["class_code"], bold=True)
    row = _write_kv(ws, row, "Pressure Rating",    ctx["rating"], bold=True)
    row = _write_kv(ws, row, "Material",           ctx["material"])
    row = _write_kv(ws, row, "Material Spec",      ctx.get("fitting_pipe", "—"))
    row = _write_kv(ws, row, "Corrosion Allowance",ctx["ca"])
    row = _write_kv(ws, row, "Service",            ctx["service"])
    return row + 1


def _build_design_conditions(ws, row: int, ctx: dict) -> int:
    row = _section_header(ws, row, "2. DESIGN CONDITIONS")
    row = _write_kv(ws, row, "Design Pressure (barg)",      f"{ctx['design_p']:.1f}")
    row = _write_kv(ws, row, "Design Pressure (psig)",      f"{_bargToPsig(ctx['design_p']):.1f}")
    row = _write_kv(ws, row, "Design Temperature (°C)",     f"{ctx['design_t']:.0f}")
    row = _write_kv(ws, row, "Design Temperature (°F)",     f"{_cToF(ctx['design_t']):.1f}")
    row = _write_kv(ws, row, "MDMT (°C)",                   f"{ctx['mdmt']:.0f}")
    row = _write_kv(ws, row, "Joint Type",                  ctx['joint_type'])
    row = _write_kv(ws, row, "Joint Efficiency (E)",        ctx['E'])
    row = _write_kv(ws, row, "Y Coefficient",               ctx['Y'])
    row = _write_kv(ws, row, "W Factor",                    ctx['W'])
    row = _write_kv(ws, row, "Mill Tolerance",              "12.5%")
    row = _write_kv(ws, row, "Hydrotest Pressure (1.5×P)",  f"{ctx['hydro_barg']:.1f} barg")
    return row + 1


def _build_pt_table(ws, row: int, ctx: dict) -> int:
    pt = ctx.get("pt")
    if not pt or not pt.get("temperatures_c"):
        return row
    row = _section_header(ws, row, "3. PRESSURE-TEMPERATURE RATING (ASME B16.5)")
    # Header row: Pressure | T1 | T2 | ...
    temps  = pt["temperatures_c"]
    labels = pt.get("temp_labels") or [str(t) for t in temps]
    presss = pt["pressures_barg"]

    ws.cell(row=row, column=1, value="Pressure (barg) →").font = FONT_LABEL
    ws.cell(row=row, column=1).fill = FILL_GRAY
    ws.cell(row=row, column=1).border = BORDER_ALL
    for i, lbl in enumerate(labels):
        c = ws.cell(row=row, column=2 + i, value=str(lbl))
        c.font = FONT_HEADER
        c.fill = FILL_HEADER
        c.alignment = CENTER
        c.border = BORDER_ALL
    row += 1

    ws.cell(row=row, column=1, value="P (barg)").font = FONT_LABEL
    ws.cell(row=row, column=1).fill = FILL_GRAY
    ws.cell(row=row, column=1).border = BORDER_ALL
    for i, p in enumerate(presss):
        c = ws.cell(row=row, column=2 + i, value=p)
        c.alignment = CENTER
        c.border = BORDER_ALL
    row += 1
    return row + 1


def _build_wall_thickness_table(ws, row: int, ctx: dict) -> int:
    row = _section_header(ws, row, "4. WALL THICKNESS CALCULATION TABLE (ASME B31.3 §304.1.2 Eq. 3a)")
    headers = ["NPS", "D (mm)", "t (mm)", "D/6 (mm)", "If t<D/6",
               "t_m (mm)", "Mill Tol", "Calc Thk T (mm)",
               "SCH", "Sel. Thk (mm)", "Status"]
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=1 + i, value=h)
        c.font = FONT_HEADER
        c.fill = FILL_HEADER
        c.alignment = CENTER
        c.border = BORDER_ALL
    ws.row_dimensions[row].height = 30
    row += 1

    fmt = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "—"
    for rrow in ctx["wt_rows"]:
        not_ok = (rrow.get("status") == "NOT OK")
        # Project rule: NOT OK → SCH blanked (—), SEL.THK echoes calc thk (2 dp).
        # Engineer must spec a custom wall ≥ calc thk for that NPS.
        if not_ok:
            sch_disp = "—"
            sel_thk_disp = f"{rrow['calc_thk_mm']:.2f}" if rrow.get("calc_thk_mm") is not None else "—"
        else:
            sch_disp = rrow.get("sch") or "—"
            sel_thk_disp = f"{rrow['sel_thk_mm']:.2f}" if rrow.get("sel_thk_mm") is not None else "—"
        cells = [
            rrow["nps"],
            f"{rrow['od_mm']:.1f}" if rrow.get("od_mm") is not None else "—",
            fmt(rrow.get("t_mm")),
            fmt(rrow.get("d_over_6")),
            rrow.get("validity") or "—",
            fmt(rrow.get("tm_mm")),
            "12.5%",
            fmt(rrow.get("calc_thk_mm")),
            sch_disp,
            sel_thk_disp,
            rrow.get("status") or "—",
        ]
        for i, v in enumerate(cells):
            c = ws.cell(row=row, column=1 + i, value=v)
            c.alignment = CENTER
            c.border = BORDER_ALL
            c.font = FONT_VALUE
            if i == 4 and v == "ALERT":
                c.fill = FILL_RED
            elif i == 10 and v == "NOT OK":
                c.fill = FILL_RED
            elif i == 10 and v == "OK":
                c.fill = FILL_GREEN
        row += 1
    return row + 1


def _build_pipe_and_fittings(ws, row: int, ctx: dict) -> int:
    fs = ctx.get("fitting_specs") or {}
    components = [
        ("Pipe",                fs.get("pipe"),           "ASTM"),
        ("90° LR Elbow",        fs.get("fittings"),       "ASME B 16.9"),
        ("45° Elbow",           fs.get("fittings"),       "ASME B 16.9"),
        ("Equal Tee",           fs.get("fittings"),       "ASME B 16.9"),
        ("Reducing Tee",        fs.get("fittings"),       "ASME B 16.9"),
        ("Concentric Reducer",  fs.get("fittings"),       "ASME B 16.9"),
        ("Eccentric Reducer",   fs.get("fittings"),       "ASME B 16.9"),
        ("Pipe Cap",            fs.get("fittings"),       "ASME B 16.9"),
        ("Plug",                fs.get("fittings"),       "Hex Head Plug, ASME B 16.11"),
        ("Weldolet",            fs.get("branch_outlet"),  "MSS SP-97"),
    ]

    row = _section_header(ws, row, "5. PIPE & FITTINGS MATERIAL ASSIGNMENT")
    headers = ["Component", "Material", "Schedule / Class", "Standard"]
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=1 + i, value=h)
        c.font = FONT_HEADER
        c.fill = FILL_HEADER
        c.alignment = CENTER
        c.border = BORDER_ALL
    row += 1

    sch_small = ctx.get("small_bore_sch", "—")
    sch_large = ctx.get("large_bore_sch", "—")

    def _write_component_row(bore_label: str, sch: str):
        cells = [
            (f"{name} ({bore_label})", FONT_LABEL,  LEFT),
            (mat or "—",               FONT_VALUE,  LEFT),
            (sch,                      FONT_VALUE,  CENTER),
            (std,                      FONT_VALUE,  LEFT),
        ]
        for i, (val, font, align) in enumerate(cells):
            c = ws.cell(row=row, column=1 + i, value=val)
            c.font = font
            c.alignment = align
            c.border = BORDER_ALL

    for name, mat, std in components:
        # Two rows per component — one for Small Bore SCH, one for Large.
        _write_component_row("Small Bore", sch_small)
        row += 1
        _write_component_row("Large Bore", sch_large)
        row += 1
    return row + 1


def _build_flange_bolts_gasket(ws, row: int, ctx: dict) -> int:
    fx = ctx.get("flange_extras") or {}
    fs = ctx.get("fitting_specs") or {}

    row = _section_header(ws, row, "6. FLANGE")
    row = _write_kv(ws, row, "MOC",            fs.get("flange"), bold=True)
    face = fx.get("face") or {}
    row = _write_kv(ws, row, "FACE",           f"{ctx['rating']}, {face.get('code', '—')} ({face.get('label', '')})")
    type_block = fx.get("type") or {}
    row = _write_kv(ws, row, "Type",           type_block.get("type"))
    row = _write_kv(ws, row, "Compact Flange", type_block.get("compact"), span=6)
    row = _write_kv(ws, row, "Hub Connector",  type_block.get("hub"), span=6)
    row += 1

    row = _section_header(ws, row, "7. BOLTS / NUTS / GASKETS")
    b = fx.get("bolting") or {}
    g = fx.get("gasket") or {}
    row = _write_kv(ws, row, "Stud Bolts",  b.get("stud"), span=6)
    row = _write_kv(ws, row, "Hex Nuts",    b.get("hex_nut"), span=6)
    row = _write_kv(ws, row, "Gasket",      g.get("spec"), span=6)
    row += 1

    # Valves — project codes per §5.5 nomenclature
    # [TYPE 2ch][SUBTYPE 1ch][SEAT 1ch][class base][FACE 1ch]
    row = _section_header(ws, row, "8. VALVES")
    v = fx.get("valves") or {}
    _vcode = lambda key: (v.get(key) or {}).get("code") if isinstance(v.get(key), dict) else None
    row = _write_kv(ws, row, "Rating",     v.get("rating"),    bold=True)
    row = _write_kv(ws, row, "Body MOC",   v.get("body"),      bold=True)
    row = _write_kv(ws, row, "Ball",       _vcode("ball"),       span=6)
    row = _write_kv(ws, row, "Gate",       _vcode("gate"),       span=6)
    row = _write_kv(ws, row, "Globe",      _vcode("globe"),      span=6)
    row = _write_kv(ws, row, "Check",      _vcode("check"),      span=6)
    if v.get("butterfly"):
        row = _write_kv(ws, row, "Butterfly", _vcode("butterfly"), span=6)
    row = _write_kv(ws, row, "DBB",        _vcode("dbb"),        span=6)
    row = _write_kv(ws, row, "DBB (Inst.)", _vcode("dbb_inst"),  span=6)
    row += 1

    row = _section_header(ws, row, "9. SPECTACLE BLIND / SPACER")
    sp = fx.get("spectacle") or {}
    row = _write_kv(ws, row, "MOC",                sp.get("moc"), bold=True)
    row = _write_kv(ws, row, "Standard (Small)",   sp.get("small_bore"))
    row = _write_kv(ws, row, "Standard (Large)",   sp.get("large_bore"))
    row += 1
    return row


def _build_branch_chart(ws, row: int, ctx: dict) -> int:
    """Section 10 — Appendix-1 Branch Connection Chart for this material.
    Renders the lower-triangular matrix (run NPS × branch NPS) with the
    project legend below."""
    chart = ctx.get("branch_chart")
    if not chart or not chart.get("matrix") or not chart.get("nps_axis"):
        return row
    title = "10. BRANCH CONNECTION CHART"
    sub = chart.get("title") or ""
    if sub:
        title = f"{title} — {sub}"
    row = _section_header(ws, row, title)

    axis = chart["nps_axis"]
    matrix = chart["matrix"]
    n = len(axis)

    def _fmt_nps(v):
        if v == 0.5:  return "1/2\""
        if v == 0.75: return "3/4\""
        if v == 1.5:  return "1-1/2\""
        return f"{v}\""

    # Subtitle row
    if chart.get("subtitle"):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=min(n + 1, 16))
        c = ws.cell(row=row, column=1, value=chart["subtitle"])
        c.font = FONT_NOTE
        c.alignment = LEFT
        row += 1
    if chart.get("resolved_family"):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=min(n + 1, 16))
        c = ws.cell(row=row, column=1, value=f"For material family: {chart['resolved_family']}")
        c.font = FONT_NOTE
        c.alignment = LEFT
        row += 1

    # Header row — corner cell + branch NPS axis
    corner = ws.cell(row=row, column=1, value="RUN ↓ / BRANCH →")
    corner.font = FONT_HEADER
    corner.fill = FILL_HEADER
    corner.alignment = CENTER
    corner.border = BORDER_HEAD
    for i, v in enumerate(axis):
        c = ws.cell(row=row, column=2 + i, value=_fmt_nps(v))
        c.font = FONT_LABEL
        c.fill = FILL_GRAY
        c.alignment = CENTER
        c.border = BORDER_ALL
    row += 1

    # Body rows
    for ridx, mrow in enumerate(matrix):
        # Run NPS label
        rc = ws.cell(row=row, column=1, value=_fmt_nps(axis[ridx]))
        rc.font = FONT_LABEL
        rc.fill = FILL_GRAY
        rc.alignment = CENTER
        rc.border = BORDER_ALL
        for cidx in range(n):
            cell = ws.cell(row=row, column=2 + cidx)
            if cidx < len(mrow):
                code = mrow[cidx]
                cell.value = code
                cell.font = FONT_VALUE_BOLD
                cell.alignment = CENTER
                cell.border = BORDER_ALL
                # Color cells by code
                if code == "T":
                    cell.fill = PatternFill("solid", fgColor="FFDBEAFE")
                elif code == "RT":
                    cell.fill = PatternFill("solid", fgColor="FFBFDBFE")
                elif code == "W":
                    cell.fill = PatternFill("solid", fgColor="FFFEF3C7")
                elif code == "S":
                    cell.fill = PatternFill("solid", fgColor="FFDCFCE7")
                elif code == "H":
                    cell.fill = PatternFill("solid", fgColor="FFFCE7F3")
                elif code == "-":
                    cell.fill = FILL_GRAY
        row += 1

    # Legend
    row += 1
    ws.cell(row=row, column=1, value="LEGEND:").font = FONT_LABEL
    for i, (code, label) in enumerate(chart.get("legend", {}).items()):
        col = 2 + i * 2
        ws.cell(row=row, column=col, value=code).font = FONT_VALUE_BOLD
        ws.cell(row=row, column=col + 1, value=label).font = FONT_VALUE
    row += 2
    return row


def _build_footer(ws, row: int, ctx: dict) -> int:
    row = _section_header(ws, row, "11. NOTES")
    notes = [
        "Calculated wall thickness per ASME B31.3 Eq. 3a; mill tolerance 12.5%.",
        "Schedule selection per ASME B36.10M §9 (or B36.19M for stainless) — lightest WT ≥ Calc Thk.",
        "Hydrotest per ASME B31.3 §345.4.2(a) — 1.5 × maximum rated pressure.",
        "Valve descriptions follow ASME conventions (body MOC + seat + bore + face); engineer maps to project valve catalog when ordering.",
        "Reviewer to verify NACE / LTCS / PWHT requirements per material and service.",
    ]
    for n in notes:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=7)
        c = ws.cell(row=row, column=1, value="• " + n)
        c.font = FONT_VALUE
        c.alignment = LEFT
        c.border = BORDER_ALL
        row += 1
    return row + 1


# ──────────────────────────────────────────────────────────────────────
# Public — orchestrate one resolve + return xlsx bytes
# ──────────────────────────────────────────────────────────────────────
def build_workbook(
    *,
    rating: str,
    material: str,
    ca: str,
    service: str,
    design_p_barg: float,
    design_t_c: float,
    mdmt_c: float,
    joint_type: str,
) -> tuple[io.BytesIO, str]:
    """Resolve the class, compute everything the UI shows, lay it out in
    an .xlsx workbook, and return (BytesIO, suggested_filename)."""

    # ---- Resolve ----------------------------------------------------------
    resolved = class_resolver.resolve(
        rating=rating, material=material, ca=ca, service=service,
    )
    class_code = resolved["class_code"]
    pt         = resolved.get("pressure_temperature") or {}
    cf         = resolved.get("code_factors") or {}
    fs         = cf.get("fitting_specs") or {}
    fx         = cf.get("flange_extras") or {}

    # ---- Code factor lookups at design temperature -----------------------
    cold_t_c = (pt.get("temperatures_c") or [38.0])[0]
    s_table   = (cf.get("stress_table") or {}).get("stress_psi_by_temp_c") or {}
    S1_psi    = _interp(s_table, cold_t_c)   if s_table else None
    S2_psi    = _interp(s_table, design_t_c) if s_table else None

    y_curve   = cf.get("y_curve") or {}
    y_temps_c = y_curve.get("temperatures_c") or []
    y_vals    = y_curve.get("y_values") or []
    y_by_temp = {str(t): v for t, v in zip(y_temps_c, y_vals) if v is not None}
    Y = _interp(y_by_temp, design_t_c) if y_by_temp else 0.4
    Y = round(Y, 2)

    W = 1.0 if design_t_c <= 510 else float("nan")
    E = _joint_eff(joint_type)
    C_mm = _parse_ca_mm(ca)
    mill_tol = 0.125

    # ---- Wall thickness rows ---------------------------------------------
    nps_list = _nps_rows(material, service)
    b3610 = _b3610_rows()
    use_ss = _uses_stainless(material)
    b3619 = _b3619_rows() if use_ss else {}
    # Primary table for the picker; fallback only applies to stainless
    # (B36.19M tops out at 80S — fall back to B36.10M for heavier walls).
    sched_table_primary  = b3619 if use_ss else b3610
    sched_table_fallback = b3610 if use_ss else None

    P1_psi = _bargToPsig((pt.get("cold_point") or {}).get("pressure_barg") or 0)
    P2_psi = _bargToPsig(design_p_barg)

    def tD(P, S):
        if S is None or P is None or not math.isfinite(W):
            return None
        return P / (2 * (S * E * W + P * Y))

    tD1, tD2 = tD(P1_psi, S1_psi), tD(P2_psi, S2_psi)
    candidates = [v for v in (tD1, tD2) if v is not None]
    tDmax = max(candidates) if candidates else None

    wt_rows = []
    for r in nps_list:
        D = r["od_mm"]
        t_mm = tDmax * D if tDmax is not None else None
        d_over_6 = D / 6
        valid = (t_mm < d_over_6) if t_mm is not None else None
        tm = t_mm + C_mm if t_mm is not None else None
        calc_thk = tm / (1 - mill_tol) if tm is not None else None
        # Project-mandated override (e.g. Titanium A70) — the NPS dim file
        # carries explicit `sch` + `wt_mm` per NPS. Use those directly.
        if r.get("sch") is not None and r.get("wt_mm") is not None:
            pick = {"sch": str(r["sch"]), "wt_mm": r["wt_mm"], "status": "OK", "fallback": False}
        else:
            pick = _pick_schedule(sched_table_primary, r["nps_decimal"], calc_thk, sched_table_fallback) if calc_thk is not None else None
        wt_rows.append({
            "nps": r["nps"], "od_mm": D, "t_mm": t_mm, "d_over_6": d_over_6,
            "validity": "OK" if valid else ("ALERT" if valid is False else None),
            "tm_mm": tm, "calc_thk_mm": calc_thk,
            "sch": pick["sch"] if pick else None,
            "sel_thk_mm": pick["wt_mm"] if pick else None,
            "status": pick["status"] if pick else None,
        })

    # ---- Bore schedules (mode across each range) --------------------------
    def _mode(rs):
        c: dict[str, int] = {}
        for x in rs:
            if x:
                c[x] = c.get(x, 0) + 1
        return max(c.items(), key=lambda kv: kv[1])[0] if c else "—"

    # Mode ignores NOT OK rows — they don't represent a standard schedule;
    # the engineer specs a custom wall ≥ calc thk on those NPS sizes.
    small_sch = _mode([r["sch"] for r in wt_rows
                       if r["nps"] and float(r["nps"]) <= 2.0 and r.get("status") == "OK"])
    large_sch = _mode([r["sch"] for r in wt_rows
                       if r["nps"] and float(r["nps"]) >= 2.5 and r.get("status") == "OK"])

    # ---- Hydrotest --------------------------------------------------------
    p_envelope = pt.get("pressures_barg") or []
    hydro_barg = (max(p_envelope) * 1.5) if p_envelope else (design_p_barg * 1.5)

    # ---- Workbook ---------------------------------------------------------
    wb = Workbook()
    ws = wb.active
    ws.title = f"PMS-{class_code}"

    # Column widths roughly match the screen layout (col A wider for labels).
    widths = [28, 22, 16, 16, 16, 16, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    # Branch chart can extend to column ~18 — give those compact widths.
    for i in range(8, 20):
        ws.column_dimensions[get_column_letter(i)].width = 8

    ctx = {
        "class_code":   class_code,
        "rating":       rating,
        "material":     material,
        "ca":           ca,
        "service":      service or "—",
        "design_p":     design_p_barg,
        "design_t":     design_t_c,
        "mdmt":         mdmt_c,
        "joint_type":   joint_type,
        "E": E, "Y": Y,
        "W": 1.0 if math.isfinite(W) else "—",
        "fitting_pipe":   fs.get("pipe"),
        "fitting_specs":  fs,
        "flange_extras":  fx,
        "branch_chart":   cf.get("branch_chart"),
        "pt":             pt,
        "wt_rows":        wt_rows,
        "small_bore_sch": small_sch,
        "large_bore_sch": large_sch,
        "hydro_barg":     hydro_barg,
    }

    row = 1
    row = _build_identification(ws, row, ctx)
    row = _build_design_conditions(ws, row, ctx)
    row = _build_pt_table(ws, row, ctx)
    row = _build_wall_thickness_table(ws, row, ctx)
    row = _build_pipe_and_fittings(ws, row, ctx)
    row = _build_flange_bolts_gasket(ws, row, ctx)
    row = _build_branch_chart(ws, row, ctx)
    row = _build_footer(ws, row, ctx)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"PMS-{class_code}_{datetime.now():%Y%m%d}.xlsx"
    return buf, filename
