"""Excel export — Datasheet-style xlsx that mirrors the on-screen Excel
View (Tab 6) row-for-row.

Header has the SP Energy logo on the left + identification block on the
right. Sections (P-T Rating, Pipe Data, Fittings Data, Flange, Blind
Flange, Spectacle/Spade, Bolts/Nuts/Gaskets, Valves, Notes) are material-
aware and follow the same per-material rules as the JS Datasheet
renderer in `app/static/js/app.js`.

Public entry: build_workbook(...) → (BytesIO, filename).
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
from openpyxl.worksheet.page import PageMargins

from app.config import settings
from app.services import (
    class_resolver,
    fitting_specs,
    flange_specs,
    pms_snapshot,
    pt_lookup,
    stress_lookup,
    y_lookup,
)


# ──────────────────────────────────────────────────────────────────────
# Styles (legacy navy palette kept for reference; new Datasheet palette
# is the DS_* block further down).
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
# Helpers — only the ones the section builders still call directly.
# Every WT-calc / schedule-pick / stress-Y interpolation helper that
# used to live here was deleted when build_workbook() switched to
# `pms_snapshot.build_pms_snapshot()`. The single source of truth for
# those formulas is now `app/services/wt_calc.py`. Any new helper
# added here must be a pure formatting / layout concern.
# ──────────────────────────────────────────────────────────────────────

def _cToF(c: float) -> float:
    return c * 9 / 5 + 32


def _load_json(name: str) -> dict:
    return json.loads((settings.data_dir / name).read_text(encoding="utf-8"))


# NPS list — material-aware and (for GRE) service-aware. Still used by
# the per-material pipe-data sections (small/large bore enumeration)
# and the tubing sub-builders; intentionally kept here.
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


# ──────────────────────────────────────────────────────────────────────
# Datasheet palette + helpers — match the on-screen Excel View (Tab 6).
# Style: black thin borders, light-gray section bars + label cells,
# white value cells. Mirrors the JS Datasheet HTML row-for-row.
# ──────────────────────────────────────────────────────────────────────
DS_BLACK     = "FF000000"
DS_BG_SECT   = "FFD9D9D9"
DS_BG_LABEL  = "FFF3F3F3"

_ds_thin = Side(style="thin", color=DS_BLACK)
DS_BORDER     = Border(left=_ds_thin, right=_ds_thin, top=_ds_thin, bottom=_ds_thin)

DS_FONT_TITLE = Font(name="Calibri", size=12, bold=True, color=DS_BLACK[2:])
DS_FONT_SECT  = Font(name="Calibri", size=10, bold=True, color=DS_BLACK[2:])
DS_FONT_LABEL = Font(name="Calibri", size=10, bold=True, color=DS_BLACK[2:])
DS_FONT_VAL   = Font(name="Calibri", size=10, color=DS_BLACK[2:])
DS_FONT_VAL_B = Font(name="Calibri", size=10, bold=True, color=DS_BLACK[2:])
DS_FONT_CODE  = Font(name="Consolas", size=10, bold=True, color=DS_BLACK[2:])

DS_FILL_SECT  = PatternFill("solid", fgColor=DS_BG_SECT)
DS_FILL_LABEL = PatternFill("solid", fgColor=DS_BG_LABEL)

DS_CENTER     = Alignment(horizontal="center", vertical="center", wrap_text=True)
DS_LEFT       = Alignment(horizontal="left",   vertical="center", wrap_text=True, indent=1)
DS_LABEL_AL   = Alignment(horizontal="left",   vertical="center", indent=1)


# ── Material detection (mirrors flange_specs / app.js helpers) ────
def _ds_is_cuni(m): return bool(m) and bool(re.search(r"CuNi|C70600|B466", m, re.I))
def _ds_is_copper(m):
    if not m: return False
    if "CUNI" in m.upper(): return False
    return bool(re.search(r"\bCOPPER\b|C12200|\bB42\b", m, re.I))
def _ds_is_gre(m): return bool(m) and bool(re.search(r"\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced", m, re.I))
def _ds_is_cpvc(m): return bool(m) and bool(re.search(r"\bCPVC\b", m, re.I))
def _ds_is_titanium(m): return bool(m) and bool(re.search(r"\bTITANIUM\b|\bTi\b|B861", m, re.I))
def _ds_is_galv(m): return bool(m) and bool(re.search(r"GALV", m, re.I))
def _ds_is_bonstrand_svc(s): return bool(s) and bool(re.search(r"Hypochlorite|BONSTRAND", s, re.I))
def _ds_is_tubing(cc, m=None):
    if cc and re.match(r"^T\d", cc.strip(), re.I): return True
    if m  and re.search(r"Tubing|N08367|6\s*MO", m, re.I): return True
    return False


def _ds_displayed_code(class_code: str, material: str, service: str) -> str:
    """GRE differentiates A50/A51/A52 by service; others use resolver code."""
    if not _ds_is_gre(material):
        return class_code or ""
    s = service or ""
    if re.search(r"Hypochlorite", s, re.I): return "A51"
    if re.search(r"Special",      s, re.I): return "A52"
    return "A50"


def _ds_fmt(v, dp=1):
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{n:.{dp}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _ds_fmt_nps(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n == 0.5:  return "0.5"
    if n == 0.75: return "0.75"
    if n == 1.5:  return "1.5"
    if n.is_integer(): return str(int(n))
    return str(n)


def _split_pipe_moc(pipe: str) -> tuple[str, str]:
    """Split a Pipe MOC string into (small-bore, large-bore) cells for
    the two-cell render in the Excel Pipe Data section.

    Input shapes (from `fitting_specs.lookup` for CS-family materials):
      • `"ASTM A 106 Gr. B / API 5L Gr. B"` — small / large bore split,
        slash separator. Any trailing parenthetical (e.g. "(hot-dip
        galvanised, ASTM A 53)") applies to both grades and is kept
        on the welded large-bore side.
      • `"API 5L Gr. X60 PSL-2"` — single spec, no slash. Used for
        X60-promoted classes (1500# / 2500# CS NACE) which are welded
        line pipe only, no seamless variant exists. Render the same
        string on both halves.

    Returns (small_text, large_text) — always a 2-tuple of strings.
    """
    if not pipe:
        return ("—", "—")
    # Split at the first "/" — keep everything before as small-bore
    # seamless spec, everything after as large-bore welded spec.
    if "/" in pipe:
        sm, _, lg = pipe.partition("/")
        return (sm.strip(), lg.strip())
    # Single spec (X60 promotion or any unsplitable string) — render
    # on both halves so the row reads consistently end-to-end.
    return (pipe.strip(), pipe.strip())


# ── Cell writers ────────────────────────────────────────────────────
def _ds_style(cell, *, font=None, fill=None, align=None, border=True):
    if font   is not None: cell.font = font
    if fill   is not None: cell.fill = fill
    if align  is not None: cell.alignment = align
    if border: cell.border = DS_BORDER


def _ds_write(ws, row, col, value, *, span=1, font=None, fill=None, align=None, border=True):
    """Write a value and optionally merge across `span` columns."""
    cell = ws.cell(row=row, column=col, value=value)
    _ds_style(cell, font=font, fill=fill, align=align, border=border)
    if span > 1:
        ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
        for c in range(col + 1, col + span):
            _ds_style(ws.cell(row=row, column=c), font=font, fill=fill, align=align, border=border)
    return cell


def _ds_section_row(ws, row, text, total_cols):
    """Full-width gray section header bar."""
    _ds_write(ws, row, 1, text, span=total_cols,
              font=DS_FONT_SECT, fill=DS_FILL_SECT, align=DS_CENTER)
    return row + 1


def _ds_label_value_row(ws, row, label, value, total_cols, *, value_bold=False, label_span=1, value_align=None):
    """Label cell (col A) + wide value cell merged across remaining cols."""
    _ds_write(ws, row, 1, label, span=label_span,
              font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    value_font = DS_FONT_VAL_B if value_bold else DS_FONT_VAL
    _ds_write(ws, row, 1 + label_span, value, span=total_cols - label_span,
              font=value_font, fill=None, align=(value_align or DS_LEFT))
    return row + 1


# ── 1. Header — logo + title + class block + Design Code/Service/Branch
def _ds_build_header(ws, row, ctx, total_cols):
    logo_cols = 2

    # The logo column (A:B) gets ONE merge spanning the full header height
    # at the end of this function. The per-row placeholder writes below
    # use span=1 (no per-row merge) — if they spanned A:B per row, the
    # final footprint merge would overlap them and Excel would flag the
    # workbook with a "content recovery" warning when opened.

    # Title row
    _ds_write(ws, row, 1, "", span=1, fill=None, align=DS_CENTER)
    _ds_write(ws, row, logo_cols + 1, "PIPING MATERIAL SPECIFICATION",
              span=total_cols - logo_cols - 2, font=DS_FONT_TITLE, align=DS_CENTER)
    _ds_write(ws, row, total_cols - 1, "Rev :",
              font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_CENTER)
    _ds_write(ws, row, total_cols, ctx.get("rev", "A0"),
              font=DS_FONT_LABEL, align=DS_CENTER)
    header_row_start = row
    row += 1

    # Class-block column splits (5 segments across the right side)
    right_cols = total_cols - logo_cols
    seg = max(2, right_cols // 5)
    cols = [logo_cols + 1 + i*seg for i in range(5)]
    spans = [seg, seg, seg, seg, total_cols - cols[4] + 1]
    labels = ["Piping Class", "Material", "C.A", "Mill Tol", "Sheet No."]

    _ds_write(ws, row, 1, "", span=1, fill=None, align=DS_CENTER)
    for c, lbl, sp in zip(cols, labels, spans):
        _ds_write(ws, row, c, lbl, span=sp,
                  font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_CENTER)
    row += 1

    # Value row — six sub-cells under five headers.
    #
    # The "Piping Class" header spans TWO value sub-cells: the class code
    # on the left, the rating on the right. The remaining four headers
    # (Material / C.A / Mill Tol / Sheet No.) each have one sub-cell.
    # This matches the project's reference PMS layout:
    #   ┌──────────── Piping Class ─────────────┬─ Material ─┬─ C.A ─┬─ Mill Tol ─┬─ Sheet No. ─┐
    #   │   A1   │   150#                       │     CS     │ 3 mm  │   12.5%    │     A1      │
    #   └────────┴──────────────────────────────┴────────────┴───────┴────────────┴─────────────┘
    is_tubing_cls = _ds_is_tubing(ctx["class_code"], ctx["material"])
    displayed_code = _ds_displayed_code(ctx["class_code"], ctx["material"], ctx.get("service"))
    # Mill tolerance:
    #   • Tubing (instrument tubing per ASTM A269) → 0.0%
    #   • GRE composite pipe → "NA" (mill tolerance doesn't apply —
    #     wall thickness is dictated by manufacturer winding spec,
    #     not by B36.10M rolling tolerance).
    #   • CuNi / Copper / CPVC also follow manufacturer standards
    #     rather than B36.10M, so they get "NA" too.
    #   • Everything else (steel grades) → 12.5% per ASME B36.10M.
    is_mfr_std_pipe = (
        _ds_is_gre(ctx["material"]) or _ds_is_cpvc(ctx["material"])
        or _ds_is_cuni(ctx["material"]) or _ds_is_copper(ctx["material"])
    )
    if is_tubing_cls:
        mill = "0.0%"
    elif is_mfr_std_pipe:
        mill = "NA"
    else:
        mill = "12.5%"
    rating_disp = "—" if is_tubing_cls else ctx["rating"]
    sheet_no = displayed_code

    # Split the "Piping Class" header span into two equal sub-cells.
    pc_left_span  = spans[0] // 2
    pc_right_span = spans[0] - pc_left_span

    _ds_write(ws, row, 1, "", span=1, fill=None, align=DS_CENTER)
    # Piping Class — left half (class code) + right half (rating).
    _ds_write(ws, row, cols[0],                 displayed_code,
              span=pc_left_span,  font=DS_FONT_VAL_B, align=DS_CENTER)
    _ds_write(ws, row, cols[0] + pc_left_span,  rating_disp,
              span=pc_right_span, font=DS_FONT_VAL_B, align=DS_CENTER)
    # Material / C.A / Mill Tol / Sheet No. each go under their header.
    _ds_write(ws, row, cols[1], ctx["material"], span=spans[1],
              font=DS_FONT_VAL_B, align=DS_CENTER)
    _ds_write(ws, row, cols[2], ctx["ca"],       span=spans[2],
              font=DS_FONT_VAL_B, align=DS_CENTER)
    _ds_write(ws, row, cols[3], mill,            span=spans[3],
              font=DS_FONT_VAL_B, align=DS_CENTER)
    _ds_write(ws, row, cols[4], sheet_no,        span=spans[4],
              font=DS_FONT_VAL_B, align=DS_CENTER)
    row += 1

    # Design Code / Service / Branch Chart
    #
    # NOTE: the Design P / Design T / MDMT / Joint Type row that used to
    # live here has been REMOVED to match the project's reference PMS
    # layout. Those values are still available to the engineer in the
    # PMS Generator UI (Tab 1, "Design Conditions") and in the JSON
    # snapshot — they just don't repeat in the Excel header anymore.
    # Design Code is material-aware:
    #   • Tubing classes have no flange-spec design code → "—"
    #   • GRE pipe is qualified under ISO 14692 / UKOOA in addition to
    #     ASME B 31.3 (matches the Bondstrand 2400 product data sheet
    #     and the project's reference PMS for A50 / A52).
    #   • NACE materials cite the sour-service standards alongside B 31.3.
    #   • Everything else is plain ASME B 31.3.
    has_nace = "NACE" in (ctx["material"] or "").upper()
    if is_tubing_cls:
        design_code = "—"
    elif _ds_is_gre(ctx["material"]):
        # Note: "ASME B31.3" (no space between B and 31.3) — matches
        # the project's reference PMS image for A50 / A51 / A52 exactly.
        # Spacing kept tight for the GRE branch even though other
        # branches use "ASME B 31.3" with a space.
        design_code = "ASME B31.3 / ISO 14692 / UKOOA"
    elif has_nace:
        design_code = "ASME B 31.3, NACE-MR-01-75 / ISO-15156-1/2/3"
    else:
        design_code = "ASME B 31.3"
    branch = ctx.get("branch_chart") or {}
    if is_tubing_cls:
        bc_label = "—"
    else:
        title = branch.get("title", "") or "Chart 1"
        # Convert "CHART-1 (CS, LTCS, SS, DSS, SDSS)" → "Chart 1".
        # The leading "CHART-" prefix becomes "Chart " (with a space),
        # and the parenthetical material-group suffix is dropped because
        # the reference PMS keeps the Branch Chart label compact.
        title = re.sub(r"^CHART[-\s]*", "Chart ", title)
        title = re.sub(r"\s*\(.*?\)\s*$", "", title)
        bc_label = f"Ref. APPENDIX-1, {title}"

    for label, value in [
        ("Design Code:",  design_code),
        ("Service:",      ctx.get("service") or "—"),
        ("Branch Chart:", bc_label),
    ]:
        _ds_write(ws, row, 1, "", span=1, fill=None, align=DS_CENTER)
        _ds_write(ws, row, logo_cols + 1, label, span=seg,
                  font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, logo_cols + 1 + seg, value, span=total_cols - (logo_cols + seg),
                  font=DS_FONT_VAL, align=DS_LEFT)
        row += 1

    # Merge the left logo footprint across all header rows
    header_row_end = row - 1
    try:
        ws.merge_cells(start_row=header_row_start, start_column=1,
                       end_row=header_row_end, end_column=logo_cols)
    except Exception:
        pass

    # Embed the logo image (PNG) in the merged A1:B(N) cell.
    #
    # Two engineering rules:
    #   1. Preserve the logo's native aspect ratio — never stretch it.
    #      (The SP Energy logo is 1630×363 px, ratio ≈ 4.49; the old
    #      hard-coded 170×90 squashed it width-wise and stretched it
    #      vertically.)
    #   2. Centre the image both vertically and horizontally inside
    #      the merged cell, leaving a small visual margin.
    #
    # We compute the pixel size of the merged area from the column
    # widths and row heights we've already set, then build a
    # OneCellAnchor anchored at A1 with EMU offsets that visually
    # centre the image.
    for r in range(header_row_start, header_row_end + 1):
        if (ws.row_dimensions[r].height or 0) < 22:
            ws.row_dimensions[r].height = 22

    from openpyxl.drawing.image import Image as XLImage
    logo_path = settings.static_dir / "images" / "logo.png"
    if logo_path.exists():
        try:
            from openpyxl.drawing.spreadsheet_drawing import (
                OneCellAnchor, AnchorMarker,
            )
            from openpyxl.drawing.xdr import XDRPositiveSize2D
            from openpyxl.utils.units import pixels_to_EMU
            from PIL import Image as PILImage

            # Native logo dimensions → aspect ratio (avoids stretching).
            with PILImage.open(logo_path) as p:
                native_w, native_h = p.size
            aspect = native_w / native_h if native_h else 1.0

            # Approximate pixel dimensions of the merged A1:B(N) area.
            # Column widths are set in `build_workbook`: A=22, B=10
            # (char units). Excel renders ~7 px per char + ~5 px padding.
            # Row heights minimum = 22 points; 1 point ≈ 4/3 px at 96 DPI.
            col_a_px = 22 * 7 + 5
            col_b_px = 10 * 7 + 5
            area_w_px = col_a_px + col_b_px
            n_rows = header_row_end - header_row_start + 1
            area_h_px = int(n_rows * 22 * 4 / 3)

            # Scale to fit inside ~88% of the area, preserving aspect.
            margin = 0.88
            target_w = int(area_w_px * margin)
            target_h = int(target_w / aspect)
            if target_h > area_h_px * margin:
                target_h = int(area_h_px * margin)
                target_w = int(target_h * aspect)

            # Centring offsets within the merged cell.
            offset_x_px = (area_w_px - target_w) // 2
            offset_y_px = (area_h_px - target_h) // 2

            img = XLImage(str(logo_path))
            img.anchor = OneCellAnchor(
                _from=AnchorMarker(
                    col=0,                       # column A (0-indexed)
                    colOff=pixels_to_EMU(offset_x_px),
                    row=header_row_start - 1,    # row 1 → index 0
                    rowOff=pixels_to_EMU(offset_y_px),
                ),
                ext=XDRPositiveSize2D(
                    cx=pixels_to_EMU(target_w),
                    cy=pixels_to_EMU(target_h),
                ),
            )
            ws.add_image(img)
        except Exception:
            # Best-effort: drop the logo rather than fail the export.
            pass

    return row


# ── 1b. Signatures block (rendered at bottom, after Notes) ───────────
def _ds_build_signatures(ws, row, ctx, total_cols):
    """Render the Prepared / Checked / Reviewed / Approved signature footer
    at the bottom of the sheet, after the Notes section.

    Layout (matches valve-datasheet style):

        ┌──────────────────┬──────────────────┬──────────────────┬──────────────────┐
        │    PREPARED      │     CHECKED      │    REVIEWED      │    APPROVED      │
        ├──────────────────┼──────────────────┼──────────────────┼──────────────────┤
        │  John Smith      │  Jane Doe        │  Pending         │  Pending         │
        ├──────────────────┼──────────────────┼──────────────────┼──────────────────┤
        │  01-Jan-2026     │  02-Jan-2026     │                  │                  │
        └──────────────────┴──────────────────┴──────────────────┴──────────────────┘

    Always renders all 4 slots — unsigned ones show "Pending".
    Colour-coded: green = APPROVED, red = REJECTED, amber = Pending.
    """
    # Build a map slot → latest non-revoked signature
    ORDER = ["PREPARED", "CHECKED", "REVIEWED", "APPROVED"]
    sigs: list[dict] = ctx.get("signatures") or []
    sig_map: dict[str, dict] = {}
    for s in sigs:
        if not s.get("revoked"):
            sig_map[s["signature_type"]] = s

    n          = len(ORDER)
    seg        = total_cols // n
    last_span  = total_cols - seg * (n - 1)  # last slot gets any leftover cols

    FILL_SIG_HEAD = PatternFill("solid", fgColor="FF0E3A5C")  # navy
    FILL_APPROVED = PatternFill("solid", fgColor="FFF0FDF4")  # green tint
    FILL_REJECTED = PatternFill("solid", fgColor="FFFEF2F2")  # red tint
    FILL_PENDING  = PatternFill("solid", fgColor="FFFFFBE6")  # amber tint

    FONT_HEAD  = Font(name="Calibri", size=9,  bold=True,  color="FFFFFFFF")
    FONT_NAME  = Font(name="Calibri", size=10, bold=True,  color="FF0F172A")
    FONT_DATE  = Font(name="Calibri", size=8,  bold=False, color="FF475569")
    AL_C       = Alignment(horizontal="center", vertical="center", wrap_text=False)

    def _slot_fill(s):
        if not s:
            return FILL_PENDING
        return FILL_APPROVED if s.get("decision") == "APPROVED" else \
               FILL_REJECTED if s.get("decision") == "REJECTED" else FILL_PENDING

    def _date_str(s) -> str:
        if not s or not s.get("signed_at"):
            return ""
        try:
            dt = datetime.fromisoformat(str(s["signed_at"]).replace("Z", "+00:00"))
            return dt.strftime("%d-%b-%Y")
        except Exception:
            return str(s["signed_at"])[:10]

    # ── Spacer before block ──
    ws.row_dimensions[row].height = 6
    row += 1

    # ── Row A: role headers (navy bar) ──
    ws.row_dimensions[row].height = 18
    for i, slot in enumerate(ORDER):
        col  = i * seg + 1
        span = seg if i < n - 1 else last_span
        _ds_write(ws, row, col, slot, span=span,
                  font=FONT_HEAD, fill=FILL_SIG_HEAD, align=AL_C)
    row += 1

    # ── Row B: name / "Pending" ──
    ws.row_dimensions[row].height = 26
    for i, slot in enumerate(ORDER):
        col   = i * seg + 1
        span  = seg if i < n - 1 else last_span
        s     = sig_map.get(slot)
        name  = (s or {}).get("signer_name_snapshot") or ""
        label = name.strip() if name.strip() else "Pending"
        _ds_write(ws, row, col, label, span=span,
                  font=FONT_NAME, fill=_slot_fill(s), align=AL_C)
    row += 1

    # ── Row C: date (empty for pending slots) ──
    ws.row_dimensions[row].height = 16
    for i, slot in enumerate(ORDER):
        col  = i * seg + 1
        span = seg if i < n - 1 else last_span
        s    = sig_map.get(slot)
        _ds_write(ws, row, col, _date_str(s), span=span,
                  font=FONT_DATE, fill=_slot_fill(s), align=AL_C)
    row += 1

    return row


# ── 2. P-T Rating ───────────────────────────────────────────────────
def _ds_build_pt(ws, row, ctx, total_cols):
    pt = ctx["pt"] or {}
    # Backend ships TWO P-T column sets in the snapshot:
    #   • `temperatures_c` / `pressures_barg` — full published curve
    #     (e.g. CS 150# goes out to 538 °C). Used by the WT calc,
    #     adequacy check, and any interpolation. The Excel/PDF
    #     should NOT print this list — it's longer than the on-screen
    #     P-T Rating table and would let the printed sheet disagree
    #     with what the engineer sees in the SPA.
    #   • `display_columns.temperatures_c` / `.pressures_barg` —
    #     backend-pre-filtered subset capped at 300 °C (the project's
    #     PT_TABLE_DISPLAY_CAP_C in pt_lookup.py). This is the exact
    #     set the SPA's P-T Rating table renders, so the printed PMS
    #     must use the same set to stay consistent.
    #
    # Fallback to the full lists only when display_columns is missing
    # (legacy snapshots from before the cap was introduced).
    display = pt.get("display_columns") or {}
    temps   = display.get("temperatures_c") or pt.get("temperatures_c") or []
    presses = display.get("pressures_barg") or pt.get("pressures_barg") or []
    labels  = display.get("temp_labels")    or pt.get("temp_labels") or [str(t) for t in temps]
    hydro   = pt.get("hydrotest_barg")
    if hydro is None:
        full_presses = pt.get("pressures_barg") or []
        hydro = (max(full_presses) * 1.5) if full_presses else ctx.get("design_p", 0) * 1.5

    title = ("Pressure-Temperature Rating (EEMUA 234, Table 69)"
             if _ds_is_titanium(ctx["material"])
             else "Pressure-Temperature Rating")
    row = _ds_section_row(ws, row, title, total_cols)

    n = len(presses)
    hydro_span = max(2, total_cols - 1 - n)

    # Pressure row — 2 dp so BONSTRAND values (16.32 etc.) keep their
    # precision. `_ds_fmt` strips trailing zeros, so cleaner B16.5
    # ratings (20.0 → "20", 19.6 → "19.6") still display tidily.
    _ds_write(ws, row, 1, "Press., barg",
              font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    for i, p in enumerate(presses):
        _ds_write(ws, row, 2 + i, _ds_fmt(p, 2), font=DS_FONT_VAL, align=DS_CENTER)
    for i in range(n, total_cols - 1 - hydro_span):
        _ds_write(ws, row, 2 + i, "", font=DS_FONT_VAL, align=DS_CENTER)
    _ds_write(ws, row, total_cols - hydro_span + 1, "Hydrotest Pr. (barg)",
              span=hydro_span, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_CENTER)
    row += 1

    # Temperature row
    _ds_write(ws, row, 1, "Temp., °C",
              font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    for i, lbl in enumerate(labels):
        _ds_write(ws, row, 2 + i, str(lbl), font=DS_FONT_VAL, align=DS_CENTER)
    for i in range(n, total_cols - 1 - hydro_span):
        _ds_write(ws, row, 2 + i, "", font=DS_FONT_VAL, align=DS_CENTER)
    # Hydrotest at 2 dp for the same reason as Press., barg above —
    # BONSTRAND hydrotest = 24.48 barg needs the extra precision.
    _ds_write(ws, row, total_cols - hydro_span + 1, _ds_fmt(hydro, 2),
              span=hydro_span, font=DS_FONT_VAL_B, align=DS_CENTER)
    row += 1

    return row


# ── Pipe TYPE / Ends per material (mirrors JS helpers) ─────────────
def _ds_pipe_type(material, service, pipe_moc=None):
    """Return (small_bore_type, large_bore_type, merge_flag) for the
    Pipe Data → TYPE row.

    `pipe_moc` lets us detect X60-promoted CS NACE classes (1500# /
    2500# routes to "API 5L Gr. X60 PSL-2") and emit LSAW-only,
    because X60 line pipe is welded-only — there's no seamless
    variant manufactured at that grade. Without this hint we'd
    print the default "Seamless / LSAW, 100% RT" split for X60
    classes, which is physically impossible.
    """
    if _ds_is_cuni(material):
        return ("Seamless", "Seam Welded", False)
    if _ds_is_copper(material):
        return ("Seamless Hard Drawn H80 (Regular)",
                "Seamless Light Drawn H55 (Regular)", False)
    if _ds_is_gre(material):
        txt = ("Manufacturer standard (BONSTRAND Series 50000C)"
               if _ds_is_bonstrand_svc(service) else "Manufacturer standard (TBA)")
        return (txt, txt, True)
    if _ds_is_titanium(material):
        return ("Seamless", "Seamless", True)
    u = (material or "").upper()
    if any(k in u for k in ("SS316", "TP316", "DSS", "SDSS")):
        return ("Seamless", "Welded, 100% RT", False)
    # X60-promoted CS NACE (1500# / 2500#): welded line pipe only,
    # no seamless variant exists. Detect via the pipe MOC string —
    # if it starts with "API 5L" without an "ASTM A 106" seamless
    # counterpart, we're on the X60 (or future X65/X70) path.
    if pipe_moc:
        u_pipe = pipe_moc.upper()
        if ("API 5L" in u_pipe and "A 106" not in u_pipe and "A106" not in u_pipe):
            return ("LSAW, 100% RT", "LSAW, 100% RT", True)
    # Plain CS / CS NACE (non-promoted) — small-bore seamless,
    # large-bore LSAW welded.
    return ("Seamless", "LSAW, 100% RT", False)


def _ds_pipe_ends(material, service):
    if _ds_is_cuni(material):
        return ("PE", "Bevel Ends", False)
    if _ds_is_copper(material):
        return ("BE", "BE", True)
    if _ds_is_gre(material):
        txt = ("Manufacturer standard (BONSTRAND Series 50000C)"
               if _ds_is_bonstrand_svc(service)
               else "Taper / Taper Socket x Spigot, Adhesive bonded")
        return (txt, txt, True)
    if _ds_is_cpvc(material):
        return ("Socket on one end", "Socket on one end", True)
    return ("BE", "BE", False)


# ── 3. Pipe Data — material-aware ──────────────────────────────────
def _ds_build_pipe_data(ws, row, ctx, total_cols):
    material   = ctx["material"]
    service    = ctx.get("service")
    class_code = ctx["class_code"]
    if _ds_is_tubing(class_code, material):
        return _ds_build_pipe_data_tubing(ws, row, ctx, total_cols)

    wt_rows = ctx["wt_rows"]
    is_gre    = _ds_is_gre(material)
    is_cpvc   = _ds_is_cpvc(material)
    is_cuni   = _ds_is_cuni(material)
    is_copper = _ds_is_copper(material)
    is_bons   = _ds_is_bonstrand_svc(service)
    data_cols = total_cols - 1

    if is_gre:
        code_label = ("Manufacturer's Std (BONSTRAND Series 50000C)"
                      if is_bons else "Manufacturer's Std.")
    elif is_cpvc:
        code_label = "ASTM F 441"
    else:
        code_label = "ASME B 36.10M"

    row = _ds_section_row(ws, row, "Pipe Data", total_cols)
    row = _ds_label_value_row(ws, row, "Code", code_label, total_cols)

    def _data_row(label, vals, *, align=DS_CENTER):
        nonlocal row
        _ds_write(ws, row, 1, label, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        for i in range(data_cols):
            v = vals[i] if i < len(vals) else ""
            _ds_write(ws, row, 2 + i, v, font=DS_FONT_VAL, align=align)
        row += 1

    sizes = [_ds_fmt_nps(r.get("nps_decimal") if r.get("nps_decimal") is not None
                                              else r["nps"]) for r in wt_rows]
    _data_row("Size(in)", sizes)

    # O.D. row — shown for all except CPVC
    if not is_cpvc:
        _data_row("O.D.mm", [_ds_fmt(r["od_mm"], 1) for r in wt_rows])

    if is_gre:
        # GRE: ID + WT from the dim file (project-supplied values)
        nps_rows = _nps_rows(material, service)
        id_by = {r["nps_decimal"]: r.get("id_mm") for r in nps_rows}
        wt_by = {r["nps_decimal"]: r.get("wt_mm") for r in nps_rows}
        _data_row("I.D. mm", [_ds_fmt(id_by.get(r["nps_decimal"]), 1) for r in wt_rows])
        _data_row("WT (mm)",  [_ds_fmt(wt_by.get(r["nps_decimal"]), 2) for r in wt_rows])
    elif is_cpvc:
        # CPVC: Sch from picker (project locks at 80), MOC + Ends + Fittings as labels.
        _data_row("Sch.", [(r.get("sch") or "—") for r in wt_rows])
        row = _ds_label_value_row(ws, row, "MOC",
            ctx["fitting_specs"].get("pipe") or "—", total_cols, value_bold=True)
        row = _ds_label_value_row(ws, row, "Ends", "Socket on one end", total_cols)
        row = _ds_label_value_row(ws, row, "Fittings", "ASTM F 439", total_cols)
        return row
    elif is_cuni or is_copper:
        # CuNi (EEMUA 234) and Copper (ASME B16.22) follow manufacturer
        # standards rather than ASME B36.10M schedule numbers. The
        # backend WT calc still produces a sel_thk_mm for each NPS
        # (using its standard picker), so we surface that thickness
        # but display "MFR-STD" in place of the schedule cell to make
        # it clear there's no B36.10M sch # to spec. Previously these
        # two rows were suppressed entirely, leaving the engineer with
        # no wall-thickness reference at all on the printed sheet.
        _data_row("Sch.",   ["MFR-STD" for _ in wt_rows])
        _data_row("WT. mm", [_ds_fmt(r.get("sel_thk_mm"), 2) for r in wt_rows])
    else:
        def _sch(r): return "—" if r.get("status") == "NOT OK" else (r.get("sch") or "—")
        def _wt(r):
            # NOT OK rows echo calc_thk rounded UP to 1 dp (backend ships the
            # pre-formatted `sel_thk_mm_display` so the SPA/Excel/HTML page
            # all show the same value). OK rows keep the schedule's exact
            # 2-decimal WT.
            if r.get("status") == "NOT OK":
                disp = r.get("sel_thk_mm_display")
                if disp:
                    return disp
                ct = r.get("calc_thk_mm")
                if ct is None:
                    return "—"
                return f"{math.ceil(ct * 10) / 10:.1f}"
            return _ds_fmt(r.get("sel_thk_mm"), 2)
        _data_row("Sch.",   [_sch(r) for r in wt_rows])
        _data_row("WT. mm", [_wt(r)  for r in wt_rows])

    # TYPE / MOC / Ends — split across small/large or merged based on material.
    # Pass the resolved pipe MOC so `_ds_pipe_type` can detect
    # X60-promoted CS NACE (1500# / 2500#) and emit LSAW-only —
    # X60 line pipe has no seamless variant.
    ptype_sm, ptype_lg, ptype_merge = _ds_pipe_type(
        material, service, pipe_moc=ctx["fitting_specs"].get("pipe"),
    )
    if ptype_merge or ptype_sm == ptype_lg:
        row = _ds_label_value_row(ws, row, "TYPE", ptype_sm, total_cols)
    else:
        sm_span = (data_cols + 1) // 2
        lg_span = data_cols - sm_span
        _ds_write(ws, row, 1, "TYPE", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, ptype_sm, span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
        _ds_write(ws, row, 2 + sm_span, ptype_lg, span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1

    # MOC
    if is_gre or is_cuni or is_copper:
        moc = ("Manufacturer standard (BONSTRAND Series 50000C)"
               if (is_gre and is_bons)
               else (ctx["fitting_specs"].get("pipe") or "—"))
        row = _ds_label_value_row(ws, row, "MOC", moc, total_cols, value_bold=True)
    else:
        pipe = ctx["fitting_specs"].get("pipe") or "—"
        mat_u = (material or "").upper()
        is_plain_cs = re.fullmatch(r"\s*CS\s*(NACE)?\s*", mat_u) and not _ds_is_galv(material)
        if is_plain_cs:
            # Plain CS (and CS NACE, and CS GALV / Epoxy-Lined which
            # fall through to this branch via the slash format) render
            # the Pipe MOC row as a small/large bore split.
            #
            # Pipe MOC strings have two shapes depending on the class:
            #
            #   1. SLASHED  — e.g. "ASTM A 106 Gr. B / API 5L Gr. B"
            #      Means small-bore seamless (A 106) / large-bore welded
            #      (API 5L). Split at the FIRST "/" and put each half
            #      in its own cell. Any trailing parenthetical applies
            #      to both halves and is preserved on the right side
            #      where the welded spec lives.
            #
            #   2. SINGLE   — e.g. "API 5L Gr. X60 PSL-2"
            #      Means the class is X60-promoted (1500#/2500# CS NACE)
            #      — X60 is welded line pipe only, no seamless variant
            #      exists. Render the same string on both halves so the
            #      row reads consistently.
            #
            # (Before this fix the right cell was hardcoded to
            # "API 5L Gr. B", which produced "X60 / Gr. B" inconsistency
            # for promoted classes and duplicated info for the slashed
            # plain-CS classes.)
            sm_text, lg_text = _split_pipe_moc(pipe)
            sm_span = (data_cols + 1) // 2
            lg_span = data_cols - sm_span
            _ds_write(ws, row, 1, "MOC", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
            _ds_write(ws, row, 2, sm_text, span=sm_span, font=DS_FONT_VAL_B, align=DS_CENTER)
            _ds_write(ws, row, 2 + sm_span, lg_text, span=lg_span,
                      font=DS_FONT_VAL_B, align=DS_CENTER)
            row += 1
        else:
            row = _ds_label_value_row(ws, row, "MOC", pipe, total_cols, value_bold=True)

    # Ends
    ends_sm, ends_lg, ends_merge = _ds_pipe_ends(material, service)
    if ends_merge or ends_sm == ends_lg:
        row = _ds_label_value_row(ws, row, "Ends", ends_sm, total_cols)
    else:
        sm_span = (data_cols + 1) // 2
        lg_span = data_cols - sm_span
        _ds_write(ws, row, 1, "Ends", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, ends_sm, span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
        _ds_write(ws, row, 2 + sm_span, ends_lg, span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1

    return row


def _ds_build_pipe_data_tubing(ws, row, ctx, total_cols):
    nps_rows = _nps_rows(ctx["material"], ctx.get("service"))
    data_cols = total_cols - 1
    if re.search(r"6\s*MO", ctx["material"], re.I):
        pipe_moc = "ASTM A269 (UNS S31254) SML, Annealed, Hardness <= 90 HRB SML"
    else:
        pipe_moc = "ASTM A269 Type 316/316L SML, Annealed, Hardness <= 90 HRB SML"

    row = _ds_section_row(ws, row, "Pipe Data", total_cols)
    row = _ds_label_value_row(ws, row, "Code", "ASTM A 269", total_cols)

    def _data_row(label, vals):
        nonlocal row
        _ds_write(ws, row, 1, label, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        for i in range(data_cols):
            v = vals[i] if i < len(vals) else ""
            _ds_write(ws, row, 2 + i, v, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1

    _data_row("Size(in)",   [_ds_fmt_nps(r["nps_decimal"]) for r in nps_rows])
    _data_row("Sch. (Thk)", [_ds_fmt(r.get("wt_mm"), 3)    for r in nps_rows])
    row = _ds_label_value_row(ws, row, "MOC",      pipe_moc, total_cols, value_bold=True)
    row = _ds_label_value_row(ws, row, "Ends",     "PE", total_cols)
    row = _ds_label_value_row(ws, row, "Fittings", "According to manufacturer standard", total_cols)
    return row


def _ds_build_fittings_tubing(ws, row, ctx, total_cols):
    nps_rows = _nps_rows(ctx["material"], ctx.get("service"))
    data_cols = total_cols - 1
    row = _ds_section_row(ws, row, "Fittings Data", total_cols)
    _ds_write(ws, row, 1, "Size(in)", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    for i in range(data_cols):
        v = _ds_fmt_nps(nps_rows[i]["nps_decimal"]) if i < len(nps_rows) else ""
        _ds_write(ws, row, 2 + i, v, font=DS_FONT_VAL, align=DS_CENTER)
    row += 1
    row = _ds_label_value_row(ws, row, "TYPE", "Compression Fitting", total_cols)
    row = _ds_label_value_row(ws, row, "MOC",
        "Compression fitting with double ferrule, body AISI 316, ferrules and nuts in AISI 316",
        total_cols, value_bold=True)
    row = _ds_label_value_row(ws, row, "Ends",
        "OD X THD, OD X OD, & OD X SW (Manufacturer Standard)", total_cols)
    return row


# ── 4. Fittings Data — material-aware ─────────────────────────────
def _ds_build_fittings(ws, row, ctx, total_cols):
    material   = ctx["material"]
    service    = ctx.get("service")
    class_code = ctx["class_code"]
    if _ds_is_tubing(class_code, material):
        return _ds_build_fittings_tubing(ws, row, ctx, total_cols)

    fs = ctx["fitting_specs"]
    fittings_moc = fs.get("fittings") or "—"
    flange_moc   = fs.get("flange") or "—"
    branch_moc   = fs.get("branch_outlet") or "—"

    is_cpvc   = _ds_is_cpvc(material)
    is_gre    = _ds_is_gre(material)
    is_copper = _ds_is_copper(material)
    is_cuni   = _ds_is_cuni(material)
    is_galv   = _ds_is_galv(material)
    is_ti     = _ds_is_titanium(material)
    is_bons   = _ds_is_bonstrand_svc(service)

    row = _ds_section_row(ws, row, "Fittings Data", total_cols)

    if is_cpvc:
        row = _ds_label_value_row(ws, row, "TYPE", "Socket Type", total_cols)
        row = _ds_label_value_row(ws, row, "MOC",  fittings_moc, total_cols, value_bold=True)
        for name in ("Elbow", "Tee", "Red.", "Cap", "Coupl."):
            row = _ds_label_value_row(ws, row, name, fittings_moc, total_cols)
        row = _ds_label_value_row(ws, row, "Union", "ASTM F 437", total_cols)
        row = _ds_label_value_row(ws, row, "TYPE",
            "Manufacturer Standard, Threaded (ASME B 1.20.1)", total_cols)
        row = _ds_label_value_row(ws, row, "MOC",
            f"{fittings_moc}; O-ring material : EPDM", total_cols)
        return row

    if is_gre:
        moc = ("Manufacturer standard (BONSTRAND Series 50000C)"
               if is_bons else fittings_moc)
        type_txt = ("Manufacturer standard (BONSTRAND Series 50000C)"
                    if is_bons
                    else "Taper / Taper Socket x Spigot, Adhesive bonded")
        row = _ds_label_value_row(ws, row, "TYPE", type_txt, total_cols)
        row = _ds_label_value_row(ws, row, "Rating",
            "Manufacturer standard (BONSTRAND Series 50000C)" if is_bons else "20 bar, 93degC",
            total_cols)
        row = _ds_label_value_row(ws, row, "MOC", moc, total_cols, value_bold=True)
        gre_rows = [
            ("Elbow",    "22.5°, 45°, 90° elbow"),
            ("Tee",      "Tee or Reducing Tee"),
            ("Mold. Tee","Molded Tee"),
            ("Red. Sad", "Reducing Saddle - Flat Face (FF)"),
            ("Reducer",  "Conc and Ecc Reducer"),
            ("Coupler",  "Coupler"),
            ("Adaptor",  "Adapter"),
        ]
        for name, gen in gre_rows:
            row = _ds_label_value_row(ws, row, name, (moc if is_bons else gen), total_cols)
        return row

    if is_ti:
        row = _ds_label_value_row(ws, row, "TYPE",
            "Butt Weld (SCH to match pipe), Seamless", total_cols)
        row = _ds_label_value_row(ws, row, "MOC", fittings_moc, total_cols, value_bold=True)
        ti_rows = [
            ("Elbow",      "ASME B 16.9 and ASME B 16.28 for short radius elbow and returns"),
            ("Tee",        "ASME B 16.9"),
            ("Red.",       "ASME B 16.9"),
            ("Cap",        "ASME B 16.9"),
            ("Plug",       "Hex Head Plug, ASME B 16.11"),
            ("Elbolet",    "MSS SP 97"),
            ("Weldolet",   "MSS SP 97"),
            ("Nipoflange", "ASTM B 363 Gr. WPT 2 (Ref. Section 1.24)"),
            ("Nipple",     "ASME B 36.10M, MOC Same as pipe"),
        ]
        for name, std in ti_rows:
            row = _ds_label_value_row(ws, row, name, std, total_cols)
        return row

    if is_copper:
        data_cols = total_cols - 1
        sm_span = (data_cols + 1) // 2
        lg_span = data_cols - sm_span
        # TYPE
        _ds_write(ws, row, 1, "TYPE", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, "Brazed Fittings (SCH to match pipe), Seamless",
                  span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
        _ds_write(ws, row, 2 + sm_span, "Butt Weld (SCH to match pipe), Seamless",
                  span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1
        # MOC + 11 component rows (MOC values vertically merged across them)
        copper_components = ["Elbow", "Tee", "Red.", "Cap", "Coupl.", "Plug",
                             "Union", "Sockolet", "Weldolet", "Nipple", "Swage"]
        moc_start = row
        moc_end   = row + len(copper_components)
        _ds_write(ws, row, 1, "MOC", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, "ASTM B 124 UNS C11000",
                  span=sm_span, font=DS_FONT_VAL_B, align=DS_CENTER)
        _ds_write(ws, row, 2 + sm_span, "ASTM B 42 UNS C12200",
                  span=lg_span, font=DS_FONT_VAL_B, align=DS_CENTER)
        try:
            ws.merge_cells(start_row=moc_start, start_column=2,
                           end_row=moc_end, end_column=1 + sm_span)
            ws.merge_cells(start_row=moc_start, start_column=2 + sm_span,
                           end_row=moc_end, end_column=total_cols)
        except Exception:
            pass
        row += 1
        for name in copper_components:
            _ds_write(ws, row, 1, name, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
            for c in range(2, total_cols + 1):
                _ds_style(ws.cell(row=row, column=c), border=True)
            row += 1
        return row

    if is_cuni:
        row = _ds_label_value_row(ws, row, "TYPE", "SW", total_cols)
        row = _ds_label_value_row(ws, row, "MOC", "90-10 Cu-Ni", total_cols, value_bold=True)
        cuni_rows = [
            ("Elbow",    "EEMUA 234"),
            ("Tee",      "EEMUA 234"),
            ("Red.",     "EEMUA 234"),
            ("Cap",      "EEMUA 234"),
            ("Coupl.",   "EEMUA 234"),
            ("Plug",     "EEMUA 234"),
            ("Union",    "EEMUA 234"),
            ("Sockolet", "EEMUA 234"),
            ("Weldolet", "EEMUA 234"),
            ("Nipple",   "EEMUA 234, MOC same as pipe"),
            ("Swage",    "EEMUA 234, MOC same as pipe"),
        ]
        for name, std in cuni_rows:
            row = _ds_label_value_row(ws, row, name, std, total_cols)
        return row

    # Standard / Galv / NACE / SS / DSS — two-bore split
    if is_galv:
        sm_type = "Screwed (SCRD), #3000"
        lg_type = "Butt Weld (SCH to match pipe), Seamless"
        sm_moc  = flange_moc
        lg_moc  = fittings_moc
        comp_rows = [
            ("Elbow",        "ASME B 16.11", "ASME B 16.9"),
            ("Tee",          "ASME B 16.11", "ASME B 16.9"),
            ("Red.",         "ASME B 16.11", "ASME B 16.9"),
            ("Cap",          "ASME B 16.11", "ASME B 16.9"),
            ("Coupl.",       "ASME B 16.11", ""),
            ("Hex Hd. Plug", "Hex Head Plug, ASME B 16.11", ""),
            ("Union",        "ASME B 16.11", "BS 3799"),
            ("Olet",         "MSS SP-97",    branch_moc),
            ("Swage",        "MSS SP-95, MOC same as pipe", None),
        ]
    else:
        sm_type = "Butt Weld (SCH to match pipe), Seamless"
        lg_type = "Butt Weld (SCH to match pipe), Welded"
        sm_moc  = fittings_moc
        lg_moc  = fittings_moc
        comp_rows = [
            ("Elbow",        "ASME B 16.9",  "ASME B 16.9"),
            ("Tee",          "ASME B 16.9",  "ASME B 16.9"),
            ("Red.",         "ASME B 16.9",  "ASME B 16.9"),
            ("Cap",          "ASME B 16.9",  "ASME B 16.9"),
            ("Hex Hd. Plug", "Hex Head Plug, ASME B 16.11", ""),
            ("Weldolet",     branch_moc,     branch_moc),
        ]

    data_cols = total_cols - 1
    sm_span = (data_cols + 1) // 2
    lg_span = data_cols - sm_span

    _ds_write(ws, row, 1, "TYPE", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    _ds_write(ws, row, 2, sm_type, span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
    _ds_write(ws, row, 2 + sm_span, lg_type, span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
    row += 1

    _ds_write(ws, row, 1, "MOC", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    _ds_write(ws, row, 2, sm_moc, span=sm_span, font=DS_FONT_VAL_B, align=DS_CENTER)
    _ds_write(ws, row, 2 + sm_span, lg_moc, span=lg_span, font=DS_FONT_VAL_B, align=DS_CENTER)
    row += 1

    for entry in comp_rows:
        name = entry[0]
        sm   = entry[1]
        lg   = entry[2] if len(entry) > 2 else ""
        _ds_write(ws, row, 1, name, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        if lg is None:
            _ds_write(ws, row, 2, sm, span=data_cols, font=DS_FONT_VAL, align=DS_CENTER)
        else:
            _ds_write(ws, row, 2, sm or "—", span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
            _ds_write(ws, row, 2 + sm_span, lg or "—", span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1
    return row


# ── 5. Flange + Blind Flange ───────────────────────────────────────
def _ds_build_flange(ws, row, ctx, total_cols):
    material = ctx["material"]
    service  = ctx.get("service")
    class_code = ctx["class_code"]
    if _ds_is_tubing(class_code, material):
        return row

    fs = ctx["fitting_specs"]
    fx = ctx["flange_extras"]
    flange_moc = fs.get("flange") or "—"
    rating_str = ctx["rating"]
    face_code  = (fx.get("face") or {}).get("code", "RF")
    face_full  = f"{rating_str} {face_code}, Serrated Finish"

    section_title = "Flange"
    if _ds_is_cpvc(material):
        section_title = "Flange (F 439, Bolt hole as per ASME B 16.5)"
    row = _ds_section_row(ws, row, section_title, total_cols)

    # TYPE — material-aware
    if _ds_is_titanium(material):
        ftype_sm = ftype_lg = "Lap Joint Flange (Note 4) / WN Flange RF"; merged = True
    elif _ds_is_cuni(material):
        ftype_sm, ftype_lg, merged = "SW Flange", "WN Flange", False
    elif _ds_is_copper(material):
        ftype_sm = ftype_lg = "Solid slip on flange"; merged = True
    elif _ds_is_cpvc(material):
        ftype_sm = ftype_lg = "#150 Socket Type/ Manufacturer Standard"; merged = True
    elif _ds_is_gre(material):
        ftype_sm = ftype_lg = ("Manufacturer standard (BONSTRAND Series 50000C)"
                                if _ds_is_bonstrand_svc(service)
                                else "Taper / Taper Socket x Spigot, Adhesive bonded")
        merged = True
    elif _ds_is_galv(material):
        ftype_sm, ftype_lg, merged = "Screwed (SCRD)", "WN", False
    else:
        # Standard B16.5 weld-neck flange. Pull the full descriptive
        # text straight from `flange_specs.flange_type()` (already
        # resolved into `ctx["flange_extras"]["type"]["type"]`) so the
        # Excel TYPE row matches what the SPA's Components tab shows —
        # e.g. "Weld Neck per ASME B 16.5, Butt Welding ends per
        # ASME B 16.25" (with the NPS-26+ note appended for ratings
        # ≥ 600#). Single source of truth — UI and Excel stay aligned.
        weld_neck_text = ((fx.get("type") or {}).get("type")
                          or "Weld Neck per ASME B 16.5, Butt Welding ends per ASME B 16.25")
        ftype_sm, ftype_lg, merged = weld_neck_text, weld_neck_text, True

    data_cols = total_cols - 1
    if merged:
        row = _ds_label_value_row(ws, row, "TYPE", ftype_sm, total_cols)
    else:
        sm_span = (data_cols + 1) // 2
        lg_span = data_cols - sm_span
        _ds_write(ws, row, 1, "TYPE", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, ftype_sm, span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
        _ds_write(ws, row, 2 + sm_span, ftype_lg, span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
        row += 1

    # MOC / FACE / STD per material
    if _ds_is_titanium(material):
        moc = "LJ=Inner Flange-B 363 WPT2, Outer Flange ASTM A 105N, Epoxy coated and WN= B 381 Gr. F2"
        std = "ASME B 16.5, Butt Welding ends as per ASME B 16.25"
        show_face = False
    elif _ds_is_cuni(material):
        moc, std, show_face = "90-10Cu-Ni", "EEMUA 234 20 BAR", False
    elif _ds_is_copper(material):
        moc, std, show_face = "ASTM B61 UNS C92200", "ASME B 16.24", True
        face_full = "FF"
    elif _ds_is_cpvc(material):
        moc, std, show_face = flange_moc, None, True
        face_full = "FF STOCK FINISH (1000 micro inch AARH)"
    elif _ds_is_gre(material):
        is_bons = _ds_is_bonstrand_svc(service)
        moc = "Manufacturer standard (BONSTRAND Series 50000C)" if is_bons else flange_moc
        std = "Drilled to ASME B 16.5, 150#" if is_bons else "Drilled to ASME B 16.5 / 16.47A, 150#"
        show_face = True
        face_full = ("Manufacturer standard (BONSTRAND Series 50000C)"
                     if is_bons else "Flat Face (FF)")
    else:
        moc, std, show_face = flange_moc, "ASME B 16.5", True

    row = _ds_label_value_row(ws, row, "MOC", moc, total_cols, value_bold=True)
    if show_face:
        row = _ds_label_value_row(ws, row, "FACE", face_full, total_cols)
    if std:
        row = _ds_label_value_row(ws, row, "STD", std, total_cols)
    return row


def _ds_build_blind_flange(ws, row, ctx, total_cols):
    material = ctx["material"]
    if _ds_is_tubing(ctx["class_code"], material):
        return row
    blind_moc = blind_type = blind_face = None
    if _ds_is_cuni(material):
        blind_moc = "ASTM A 105N FF with 3mm 90-10 CuNi weld deposit"
    elif _ds_is_copper(material):
        blind_moc = "ASTM A 105N RF With 3mm Copper over lay"
    elif _ds_is_cpvc(material):
        blind_type = "#150 / Manufacturer Standard"
        blind_moc  = ctx["fitting_specs"].get("flange") or "—"
        blind_face = "FF STOCK FINISH (1000 micro inch AARH)"
    elif _ds_is_titanium(material):
        blind_moc = "ASTM B 381 Gr. F 2 as per ASME B 16.5"
    if not blind_moc:
        return row
    row = _ds_section_row(ws, row, "Blind Flange", total_cols)
    if blind_type:
        row = _ds_label_value_row(ws, row, "TYPE", blind_type, total_cols)
    row = _ds_label_value_row(ws, row, "MOC", blind_moc, total_cols)
    if blind_face:
        row = _ds_label_value_row(ws, row, "FACE", blind_face, total_cols)
    return row


# ── 6. Spectacle Blind / Spade and Spacer ─────────────────────────
def _ds_build_spectacle(ws, row, ctx, total_cols):
    material = ctx["material"]
    if _ds_is_tubing(ctx["class_code"], material):
        return row
    if _ds_is_gre(material):
        row = _ds_section_row(ws, row, "Spade and Spacer", total_cols)
        row = _ds_label_value_row(ws, row, "TYPE",
            "Manufacturer standard, Flat Face (FF)", total_cols)
        return row
    if _ds_is_copper(material) or _ds_is_cpvc(material) or _ds_is_titanium(material):
        return row
    sp = ctx["flange_extras"].get("spectacle") or {}
    row = _ds_section_row(ws, row, "Spectacle Blind/Spacer Blinds", total_cols)
    row = _ds_label_value_row(ws, row, "MOC",
        sp.get("moc") or ctx["fitting_specs"].get("flange") or "—",
        total_cols, value_bold=True)
    data_cols = total_cols - 1
    sm_span = (data_cols + 1) // 2
    lg_span = data_cols - sm_span
    _ds_write(ws, row, 1, "Spectacle", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    _ds_write(ws, row, 2, sp.get("small_bore") or "—",
              span=sm_span, font=DS_FONT_VAL, align=DS_CENTER)
    _ds_write(ws, row, 2 + sm_span, sp.get("large_bore") or "—",
              span=lg_span, font=DS_FONT_VAL, align=DS_CENTER)
    row += 1
    return row


# ── 7. Bolts/Nuts/Gaskets (Mechanical Joints) ──────────────────────
def _ds_build_bolts_gaskets(ws, row, ctx, total_cols):
    material = ctx["material"]
    if _ds_is_tubing(ctx["class_code"], material):
        return row
    fx = ctx["flange_extras"]
    bolting = fx.get("bolting") or {}
    gasket  = fx.get("gasket") or {}
    title = ("Mechanical Joints"
             if (_ds_is_copper(material) or _ds_is_cpvc(material))
             else "Bolts/ Nuts/ Gaskets")
    row = _ds_section_row(ws, row, title, total_cols)
    row = _ds_label_value_row(ws, row, "Stud Bolts", bolting.get("stud") or "—", total_cols)
    row = _ds_label_value_row(ws, row, "Hex Nuts",   bolting.get("hex_nut") or "—", total_cols)
    if _ds_is_gre(material):
        row = _ds_label_value_row(ws, row, "Washers", "ASTM A 307 Gr. B HDG", total_cols)
    specs = gasket.get("specs") or [gasket.get("spec") or "—"]
    for g in specs:
        row = _ds_label_value_row(ws, row, "Gasket", g, total_cols)
    return row


# ── 8. Valves ──────────────────────────────────────────────────────
def _ds_build_valves(ws, row, ctx, total_cols):
    material = ctx["material"]
    service  = ctx.get("service")
    fx = ctx["flange_extras"]
    v  = fx.get("valves") or {}
    is_tubing = _ds_is_tubing(ctx["class_code"], material)
    if _ds_is_gre(material) and _ds_is_bonstrand_svc(service):
        return row  # A51 hides Valves

    row = _ds_section_row(ws, row, "Valves", total_cols)

    def _vrow(label, code_key):
        nonlocal row
        item = v.get(code_key) or {}
        code = item.get("code", "—") if isinstance(item, dict) else "—"
        _ds_write(ws, row, 1, label, font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
        _ds_write(ws, row, 2, code, span=total_cols - 1, font=DS_FONT_CODE, align=DS_CENTER)
        row += 1

    _ds_write(ws, row, 1, "Rating", font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_LABEL_AL)
    _ds_write(ws, row, 2, v.get("rating") or "—",
              span=total_cols - 1, font=DS_FONT_VAL, align=DS_CENTER)
    row += 1

    if is_tubing:
        _vrow("DBB (Inst)",    "dbb_inst")
        _vrow("Needle (Inst)", "needle")
        _vrow("Ball (Inst)",   "ball")
        _vrow("Check (Inst)",  "check")
    else:
        _vrow("Ball",   "ball")
        _vrow("Gate",   "gate")
        _vrow("Globe",  "globe")
        _vrow("Check",  "check")
        if v.get("butterfly"): _vrow("Butterfly", "butterfly")
        if v.get("dbb"):       _vrow("DBB",        "dbb")
        if v.get("dbb_inst"):  _vrow("DBB (Inst.)", "dbb_inst")
        if v.get("needle"):    _vrow("Needle",     "needle")
    return row


# ── 9. Notes ──────────────────────────────────────────────────────
# Notes content moved to `app/data/pms_notes.json`; see
# `wt_calc.resolve_project_notes()`. The snapshot already exposes the
# filtered list as `project_notes` (preferred). For legacy callers /
# safety we fall back to the unfiltered text list.

def _ds_build_notes(ws, row, ctx, total_cols):
    row = _ds_section_row(ws, row, "NOTES", total_cols)
    notes: list[dict] = list(ctx.get("project_notes") or [])
    if not notes:
        # Fallback: unfiltered text list if the snapshot didn't carry one.
        from app.services import wt_calc as _wt
        notes = [{"id": i + 1, "text": t}
                 for i, t in enumerate(_wt.project_notes_texts())]
    for n in notes:
        _ds_write(ws, row, 1, str(n.get("id") or ""),
                  font=DS_FONT_LABEL, fill=DS_FILL_LABEL, align=DS_CENTER)
        _ds_write(ws, row, 2, n.get("text") or "", span=total_cols - 1,
                  font=DS_FONT_VAL, align=DS_LEFT)
        row += 1
    return row


# ──────────────────────────────────────────────────────────────────────
# Public entry — Datasheet-style workbook
# ──────────────────────────────────────────────────────────────────────
def build_workbook_obj(
    *,
    rating: str,
    material: str,
    ca: str,
    service: str,
    design_p_barg: float,
    design_t_c: float,
    mdmt_c: float,
    joint_type: str,
) -> tuple["Workbook", str]:
    """Build the Datasheet-style Workbook in memory without serialising.

    Returns (wb, class_code). Callers that need the xlsx bytes use
    `build_workbook()` (thin wrapper below); callers that want to
    inspect cells / styles / merges to render another format (e.g.
    `pdf_exporter` → ReportLab) call this directly to avoid the
    save → load round-trip.

    Architecture: this function does NO engineering math itself. It
    calls `pms_snapshot.build_pms_snapshot()` — the single source of
    truth used by `/api/compute-pms` and `/api/pms-agent/save` — and
    feeds that snapshot into the existing section-builder functions
    (`_ds_build_*`) which handle pure Excel formatting.

    Consequence: every value in the downloaded Excel matches what the
    SPA renders on screen, by construction. Future changes to the
    B31.3 calc engine, flag rules, materials tab, etc. reflect in the
    Excel (and the PDF, which is rendered from this same workbook)
    automatically — no parallel updates required."""

    # ── 1. Build the canonical snapshot — same as /api/compute-pms ──
    snapshot = pms_snapshot.build_pms_snapshot(
        rating=rating,
        material=material,
        corrosion_allowance=ca,
        service=service or "",
        design_pressure_barg=design_p_barg,
        design_temp_c=design_t_c,
        mdmt_c=mdmt_c,
        joint_type=joint_type,
    )
    class_code = snapshot["class_code"]
    pt         = snapshot.get("pressure_temperature") or {}
    cf         = snapshot.get("code_factors") or {}
    fs         = cf.get("fitting_specs") or {}
    fx         = cf.get("flange_extras") or {}
    eff        = snapshot.get("effective_design_conditions") or {}

    # ── 2. Adapt snapshot.wall_thickness.rows → the ctx shape the
    #       existing section builders expect (sch_display→sch,
    #       sch_status→status). Snapshot is the source of truth.
    snap_rows = (snapshot.get("wall_thickness") or {}).get("rows") or []
    wt_rows = [
        {
            "nps":                r.get("nps"),
            "nps_decimal":        r.get("nps_decimal"),
            "od_mm":              r.get("od_mm"),
            "t_mm":                r.get("t_mm"),
            "d_over_6":           r.get("d_over_6"),
            "validity":           r.get("validity"),
            "tm_mm":              r.get("tm_mm"),
            "calc_thk_mm":        r.get("calc_thk_mm"),
            "sch":                r.get("sch_display"),
            "sel_thk_mm":         r.get("sel_thk_mm"),
            # Pre-formatted display string (ceil to 1 dp) for NOT OK rows.
            "sel_thk_mm_display": r.get("sel_thk_mm_display"),
            "status":             r.get("sch_status"),
        }
        for r in snap_rows
    ]

    # ── 3. Total columns — driven by NPS axis length + label column.
    nps_count   = max(len(wt_rows), 7)
    total_cols  = max(nps_count + 1, 12)

    wb = Workbook()
    ws = wb.active
    # Excel sheet titles forbid these chars: : \ / ? * [ ]
    # `class_code` can include "[" / "]" for customized PMS variants
    # (e.g. "New-spec-[A1]"), so sanitise before assigning. We also
    # cap at 31 chars (Excel's title length limit).
    _safe_sheet_name = re.sub(r"[\[\]:\\/?*]", "", f"PMS-{class_code}")[:31]
    ws.title = _safe_sheet_name

    ws.column_dimensions[get_column_letter(1)].width = 22
    for i in range(2, total_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 10

    # Design conditions: prefer the user-supplied values; fall back to
    # the effective values the snapshot applied when nulls came in.
    ctx = {
        "class_code":    class_code,
        "rating":        rating,
        "material":      material,
        "ca":            ca,
        "service":       (service or "").strip() or "—",
        "design_p":      design_p_barg if design_p_barg is not None else eff.get("design_pressure_barg"),
        "design_t":      design_t_c    if design_t_c    is not None else eff.get("design_temp_c"),
        "mdmt":          mdmt_c        if mdmt_c        is not None else eff.get("mdmt_c"),
        "joint_type":    joint_type    or eff.get("joint_type"),
        "fitting_specs": fs,
        "flange_extras": fx,
        "branch_chart":  cf.get("branch_chart"),
        "pt":            pt,
        "wt_rows":       wt_rows,
        # Surface the rest of the snapshot too in case future section
        # builders want to render adequacy / flags / materials_tab /
        # derived conditions. Today's builders don't use these yet but
        # the data is here when they're extended.
        "adequacy":           snapshot.get("adequacy"),
        "derived_conditions": snapshot.get("derived_conditions"),
        "wt_summary":         (snapshot.get("wall_thickness") or {}).get("summary"),
        "wt_flags":           (snapshot.get("wall_thickness") or {}).get("flags"),
        "materials_tab":      snapshot.get("materials_tab"),
        # Project standard notes — filtered list from the snapshot
        # (single source of truth is `pms_notes.json`).
        "project_notes":      snapshot.get("project_notes") or [],
        "rev":           "A0",
    }

    row = 1
    row = _ds_build_header(ws, row, ctx, total_cols)
    row = _ds_build_pt(ws, row, ctx, total_cols)
    row = _ds_build_pipe_data(ws, row, ctx, total_cols)
    row = _ds_build_fittings(ws, row, ctx, total_cols)
    row = _ds_build_flange(ws, row, ctx, total_cols)
    row = _ds_build_blind_flange(ws, row, ctx, total_cols)
    row = _ds_build_spectacle(ws, row, ctx, total_cols)
    row = _ds_build_bolts_gaskets(ws, row, ctx, total_cols)
    row = _ds_build_valves(ws, row, ctx, total_cols)
    row = _ds_build_notes(ws, row, ctx, total_cols)

    # ── Print / page setup ───────────────────────────────────────────
    # The PMS datasheet is 23 columns wide — it never fits on a default
    # portrait Letter page, so without this block the PDF (rendered by
    # LibreOffice from this .xlsx) gets the right-hand columns chopped
    # off. The settings below tell Excel/LibreOffice exactly how to
    # paginate so the printed/PDF output mirrors the on-screen layout:
    #
    #   • A4 landscape — natural fit for our column count.
    #   • fitToWidth=1, fitToHeight=0 — scale every page to one page
    #     wide; let the height flow naturally across as many pages as
    #     the content needs.
    #   • Narrow margins — eliminates the big white borders the user
    #     was seeing on every side.
    #   • Horizontal centering — keeps the table centred on the page
    #     when the scaled content is slightly narrower than full width.
    #   • Explicit print_area covering everything we wrote — so blank
    #     trailing rows / columns don't get pulled into the print job.
    final_row = row - 1
    last_col_letter = get_column_letter(total_cols)
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize   = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(
        left=0.25, right=0.25,
        top=0.3,   bottom=0.3,
        header=0.15, footer=0.15,
    )
    ws.print_options.horizontalCentered = True
    ws.print_area = f"A1:{last_col_letter}{final_row}"

    return wb, class_code


def build_workbook_from_snapshot(
    snapshot: dict,
    rev_code: str = "A0",
    signatures: Optional[list] = None,
) -> tuple["Workbook", str]:
    """Build the Datasheet workbook directly from a **frozen** snapshot dict
    (as stored in `pms_snapshots.payload`).

    Unlike `build_workbook_obj` this function does NOT re-run the PMS engine.
    It is the correct entry-point for revision downloads — the frozen payload
    IS the canonical data for that revision (including any edits the user made
    to service / design-pressure / design-temp after creation).

    Returns (wb, class_code).
    """
    p   = snapshot
    dc  = p.get("design_conditions") or {}
    eff = p.get("effective_design_conditions") or {}
    cf  = p.get("code_factors") or {}
    fs  = cf.get("fitting_specs") or {}
    fx  = cf.get("flange_extras") or {}
    pt  = p.get("pressure_temperature") or {}

    def _pick(*vals):
        for v in vals:
            if v not in (None, ""):
                return v
        return None

    class_code = p.get("class_code") or p.get("base_class_code") or "PMS"

    snap_rows = (p.get("wall_thickness") or {}).get("rows") or []
    wt_rows = [
        {
            "nps":                r.get("nps"),
            "nps_decimal":        r.get("nps_decimal"),
            "od_mm":              r.get("od_mm"),
            "t_mm":               r.get("t_mm"),
            "d_over_6":           r.get("d_over_6"),
            "validity":           r.get("validity"),
            "tm_mm":              r.get("tm_mm"),
            "calc_thk_mm":        r.get("calc_thk_mm"),
            "sch":                r.get("sch_display"),
            "sel_thk_mm":         r.get("sel_thk_mm"),
            "sel_thk_mm_display": r.get("sel_thk_mm_display"),
            "status":             r.get("sch_status"),
        }
        for r in snap_rows
    ]

    nps_count  = max(len(wt_rows), 7)
    total_cols = max(nps_count + 1, 12)

    wb = Workbook()
    ws = wb.active
    _safe_sheet_name = re.sub(r"[\[\]:\\/?*]", "", f"PMS-{class_code}")[:31]
    ws.title = _safe_sheet_name

    ws.column_dimensions[get_column_letter(1)].width = 22
    for i in range(2, total_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 10

    # Design conditions — top-level fields first (written by upsert_snapshot),
    # then fall back to nested sub-dicts (older snapshots).
    design_p = _pick(p.get("design_pressure_barg"), dc.get("design_pressure_barg"), eff.get("design_pressure_barg"))
    design_t = _pick(p.get("design_temp_c"),        dc.get("design_temp_c"),        eff.get("design_temp_c"))
    mdmt     = _pick(p.get("mdmt_c"),               dc.get("mdmt_c"),               eff.get("mdmt_c"))
    joint    = _pick(p.get("joint_type"),            dc.get("joint_type"),           eff.get("joint_type")) or "Seamless"
    service  = _pick(p.get("service"),               dc.get("service")) or ""

    ctx = {
        "class_code":    class_code,
        "rating":        _pick(p.get("rating")),
        "material":      _pick(p.get("material")),
        "ca":            _pick(p.get("corrosion_allowance"), p.get("digit")),
        "service":       service.strip() or "—",
        "design_p":      design_p,
        "design_t":      design_t,
        "mdmt":          mdmt,
        "joint_type":    joint,
        "fitting_specs": fs,
        "flange_extras": fx,
        "branch_chart":  cf.get("branch_chart"),
        "pt":            pt,
        "wt_rows":       wt_rows,
        "adequacy":           p.get("adequacy"),
        "derived_conditions": p.get("derived_conditions"),
        "wt_summary":         (p.get("wall_thickness") or {}).get("summary"),
        "wt_flags":           (p.get("wall_thickness") or {}).get("flags"),
        "materials_tab":      p.get("materials_tab"),
        "project_notes":      p.get("project_notes") or [],
        "rev":           rev_code,
        "signatures":    signatures or [],
    }

    row = 1
    row = _ds_build_header(ws, row, ctx, total_cols)
    row = _ds_build_pt(ws, row, ctx, total_cols)
    row = _ds_build_pipe_data(ws, row, ctx, total_cols)
    row = _ds_build_fittings(ws, row, ctx, total_cols)
    row = _ds_build_flange(ws, row, ctx, total_cols)
    row = _ds_build_blind_flange(ws, row, ctx, total_cols)
    row = _ds_build_spectacle(ws, row, ctx, total_cols)
    row = _ds_build_bolts_gaskets(ws, row, ctx, total_cols)
    row = _ds_build_valves(ws, row, ctx, total_cols)
    row = _ds_build_notes(ws, row, ctx, total_cols)
    row = _ds_build_signatures(ws, row, ctx, total_cols)

    final_row = row - 1
    last_col_letter = get_column_letter(total_cols)
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize   = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth  = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(
        left=0.25, right=0.25,
        top=0.3,   bottom=0.3,
        header=0.15, footer=0.15,
    )
    ws.print_options.horizontalCentered = True
    ws.print_area = f"A1:{last_col_letter}{final_row}"

    return wb, class_code


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
    """Serialise the in-memory Workbook to xlsx bytes for download.

    Thin wrapper over `build_workbook_obj()` — call that directly if
    you need the Workbook object itself (e.g. for PDF conversion)."""
    wb, class_code = build_workbook_obj(
        rating=rating, material=material, ca=ca, service=service,
        design_p_barg=design_p_barg, design_t_c=design_t_c,
        mdmt_c=mdmt_c, joint_type=joint_type,
    )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"PMS-{class_code}_{datetime.now():%Y%m%d}.xlsx"
    return buf, filename
