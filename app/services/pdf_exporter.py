"""POST /api/export/pdf — pure-Python PDF rendering of the PMS datasheet.

The previous implementation shelled out to LibreOffice (`soffice --headless
--convert-to pdf`). That gave pixel-identical fidelity to the Excel but
needed LibreOffice installed on the host — which doesn't play well with
Render's Python runtime. This implementation is **pure Python with zero
system dependencies**:

  1. Call `excel_exporter.build_workbook_obj()` to get the same openpyxl
     Workbook the Excel download uses — single source of truth for
     layout, values, merges, and styles.
  2. Walk that Workbook's active sheet cell-by-cell, translating
     openpyxl's cell values + merges + fonts + fills + alignments into
     a ReportLab Table flowable.
  3. Scale the column widths to fit a landscape A4 page and render the
     Table inside a `SimpleDocTemplate`.

Trade-off vs the LibreOffice approach: the PDF isn't byte-for-byte the
same image (different font hinting, Calibri → Helvetica fallback) but
the layout, values, merges, fills, and section structure all match the
Excel. The user gets a clean printable PDF that says the same things in
the same places — and there's nothing to install on Render."""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from openpyxl.workbook import Workbook
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Image as RLImage,
)

from app.config import settings
from app.services import excel_exporter


# ── Unit conversions ───────────────────────────────────────────────

# openpyxl column widths are in "character units" (≈ width of digit "0"
# in the default font). Empirical pixel mapping: 1 char ≈ 7 px + 5 px
# padding. 1 px @ 96 DPI = 0.75 pt. So 1 char ≈ 7 * 0.75 = 5.25 pt.
_CHAR_TO_PT = 5.25

# Row heights in openpyxl are in points already — no conversion needed.

# ReportLab font fallback. The Excel uses Calibri / Arial / similar
# Microsoft fonts that aren't bundled with ReportLab. Helvetica is the
# closest free metric-compatible substitute that ReportLab ships with
# by default — looks clean and renders at the same widths.
_DEFAULT_FONT      = "Helvetica"
_DEFAULT_FONT_BOLD = "Helvetica-Bold"


# ── Public entry point ──────────────────────────────────────────────

def build_pdf(
    *,
    rating: str,
    material: str,
    ca: str,
    service: str = "",
    design_p_barg: float,
    design_t_c: float,
    mdmt_c: float = -29,
    joint_type: str = "Seamless",
) -> tuple[io.BytesIO, str]:
    """Generate a PDF of the PMS datasheet. Same call signature as
    `excel_exporter.build_workbook` so the route layer streams it the
    same way."""
    # 1. Build the canonical Workbook (also drives the Excel download).
    wb, class_code = excel_exporter.build_workbook_obj(
        rating=rating, material=material, ca=ca, service=service,
        design_p_barg=design_p_barg, design_t_c=design_t_c,
        mdmt_c=mdmt_c, joint_type=joint_type,
    )

    # 2. Convert Workbook → PDF via ReportLab.
    pdf_bytes = _workbook_to_pdf_bytes(wb)

    # 3. Filename mirrors the Excel one but with .pdf.
    from datetime import datetime
    filename = f"PMS-{class_code}_{datetime.now():%Y%m%d}.pdf"
    return io.BytesIO(pdf_bytes), filename


# ── Workbook → PDF rendering ───────────────────────────────────────

def _workbook_to_pdf_bytes(wb: Workbook) -> bytes:
    """Render the active worksheet of `wb` as a single-table PDF
    using ReportLab. Preserves merges, fills, fonts, and alignment."""
    ws = wb.active
    max_row = ws.max_row
    max_col = ws.max_column

    # ── Page geometry ──
    # Landscape A4 with the same narrow margins we baked into the xlsx
    # print setup, so the PDF feels visually consistent with what
    # Excel/LibreOffice would print.
    page_w, page_h = landscape(A4)
    margin_pt = 18  # 0.25 inch
    avail_w = page_w - 2 * margin_pt

    # ── Column widths (scaled to fit page width) ──
    raw_widths_pt = []
    for c in range(1, max_col + 1):
        letter = get_column_letter(c)
        col_dim = ws.column_dimensions.get(letter)
        char_width = (col_dim.width if (col_dim and col_dim.width) else 10.0)
        raw_widths_pt.append(char_width * _CHAR_TO_PT)
    total_raw = sum(raw_widths_pt) or avail_w
    scale = min(1.0, avail_w / total_raw)
    col_widths_pt = [w * scale for w in raw_widths_pt]

    # ── Row heights (scaled proportionally) ──
    row_heights_pt = []
    for r in range(1, max_row + 1):
        rd = ws.row_dimensions.get(r)
        h = (rd.height if (rd and rd.height) else 14.0)
        row_heights_pt.append(h * scale)

    # ── 2D cell-value array ──
    data: list[list] = [
        [_cell_value_str(ws.cell(row=r, column=c)) for c in range(1, max_col + 1)]
        for r in range(1, max_row + 1)
    ]

    # ── Embed the logo image into the A1 cell ──
    # The Excel exporter anchors the logo at A1 inside a merged A1:B(N)
    # range. We replace cell (0, 0) of the data array with a ReportLab
    # Image flowable sized to fit the merged area — the SPAN style
    # below (derived from ws.merged_cells) makes the image visually
    # occupy the full merge.
    _inject_logo(ws, data, col_widths_pt, row_heights_pt, scale)

    # ── Table styles ──
    style_cmds: list = [
        # Defaults applied to every cell.
        ("GRID",     (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, -1), _DEFAULT_FONT),
        ("FONTSIZE", (0, 0), (-1, -1), max(6.5, 10 * scale)),
        ("VALIGN",   (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
    ]

    # SPAN every merged-cell range (ReportLab uses (col, row), 0-indexed).
    for mr in ws.merged_cells.ranges:
        style_cmds.append((
            "SPAN",
            (mr.min_col - 1, mr.min_row - 1),
            (mr.max_col - 1, mr.max_row - 1),
        ))

    # Per-cell font / fill / alignment overrides.
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            cell_xy = (c - 1, r - 1)

            # Font: bold + colour. (Font name we always pin to Helvetica
            # since Calibri isn't bundled with ReportLab — substituting
            # at the cell level keeps things consistent.)
            f = cell.font
            if f:
                if f.bold:
                    style_cmds.append(("FONTNAME", cell_xy, cell_xy, _DEFAULT_FONT_BOLD))
                if f.size:
                    style_cmds.append(("FONTSIZE", cell_xy, cell_xy, max(6.0, float(f.size) * scale)))
                text_color = _argb_to_rl_color(getattr(f.color, "rgb", None))
                if text_color is not None:
                    style_cmds.append(("TEXTCOLOR", cell_xy, cell_xy, text_color))

            # Fill (background).
            fl = cell.fill
            if fl and fl.fgColor:
                bg = _argb_to_rl_color(getattr(fl.fgColor, "rgb", None))
                # Skip pure black (openpyxl's default for "no fill" can
                # serialise as "00000000" — we don't want that as a fill).
                if bg is not None and bg != colors.black:
                    style_cmds.append(("BACKGROUND", cell_xy, cell_xy, bg))

            # Horizontal alignment.
            al = cell.alignment
            if al and al.horizontal:
                if al.horizontal == "center":
                    style_cmds.append(("ALIGN", cell_xy, cell_xy, "CENTER"))
                elif al.horizontal == "right":
                    style_cmds.append(("ALIGN", cell_xy, cell_xy, "RIGHT"))
                elif al.horizontal == "left":
                    style_cmds.append(("ALIGN", cell_xy, cell_xy, "LEFT"))

    # ── Build the Table + PDF document ──
    table = Table(data, colWidths=col_widths_pt, rowHeights=row_heights_pt, repeatRows=0)
    table.setStyle(TableStyle(style_cmds))

    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(
        pdf_buf,
        pagesize=landscape(A4),
        leftMargin=margin_pt, rightMargin=margin_pt,
        topMargin=margin_pt,  bottomMargin=margin_pt,
        title=f"PMS-{ws.title}",
        author="PMS Generator",
    )
    doc.build([table])
    return pdf_buf.getvalue()


# ── Helpers ────────────────────────────────────────────────────────

def _cell_value_str(cell) -> str:
    """Format an openpyxl cell value for display, respecting its
    `number_format` when sensible (so floats don't render as
    '3.7299999999999995' etc)."""
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        # Honour the number_format precision if we can parse it.
        fmt = (cell.number_format or "").strip()
        if isinstance(v, float):
            decimals = _decimals_from_format(fmt)
            if decimals is not None:
                return f"{v:.{decimals}f}"
            # Round to 4 dp by default — enough to be precise without
            # showing IEEE-754 noise.
            return f"{v:.4g}"
        return str(v)
    return str(v)


def _decimals_from_format(fmt: str) -> Optional[int]:
    """Extract decimal-place count from an Excel number format string
    like '0.00' (→ 2) or '#,##0.0##' (→ 1, taking only mandatory dp).
    Returns None for non-numeric formats."""
    if not fmt or fmt.lower() == "general":
        return None
    m = re.search(r"0\.(0+)", fmt)
    if m:
        return len(m.group(1))
    if "." in fmt and any(ch in fmt for ch in "0#"):
        return 0
    return None


def _argb_to_rl_color(rgb_val) -> Optional[colors.Color]:
    """Convert an openpyxl ARGB / RGB hex string to a ReportLab Color.
    Returns None when the value isn't a usable hex string (e.g. it's a
    theme reference like '<TYPE=THEME>')."""
    if not rgb_val or not isinstance(rgb_val, str):
        return None
    s = rgb_val.strip().upper().lstrip("#")
    if len(s) == 8:    # ARGB
        s = s[2:]      # drop alpha
    if len(s) != 6 or any(ch not in "0123456789ABCDEF" for ch in s):
        return None
    return colors.HexColor("#" + s)


def _inject_logo(ws, data, col_widths_pt, row_heights_pt, scale) -> None:
    """Place the logo PNG into cell (0, 0) of the data array, sized to
    fit the merged A1:B(N) header area. The Excel writes the logo as an
    image anchored to A1 inside a merged range; the ReportLab Table
    represents that merge via SPAN, so the image rendered in (0, 0)
    visually occupies the whole merged cell."""
    logo_path: Path = settings.static_dir / "images" / "logo.png"
    if not logo_path.exists():
        return

    # Find the merged range that contains A1, if any — gives us the
    # exact pixel footprint the logo should fill.
    merge_bottom_row = 0
    merge_right_col = 0
    for mr in ws.merged_cells.ranges:
        if mr.min_row == 1 and mr.min_col == 1:
            merge_bottom_row = mr.max_row
            merge_right_col  = mr.max_col
            break
    if merge_bottom_row == 0:
        # No merge — fall back to a sensible default header height.
        merge_bottom_row = 6
        merge_right_col  = 2

    area_w_pt = sum(col_widths_pt[:merge_right_col])
    area_h_pt = sum(row_heights_pt[:merge_bottom_row])

    # Native aspect ratio so we never stretch.
    try:
        with PILImage.open(logo_path) as p:
            native_w, native_h = p.size
        aspect = (native_w / native_h) if native_h else 1.0
    except Exception:
        aspect = 4.5  # safe fallback for SP-Energy banner-style logos

    # Fit inside 90 % of the area, preserving aspect ratio.
    fit_w = area_w_pt * 0.9
    fit_h = fit_w / aspect
    if fit_h > area_h_pt * 0.9:
        fit_h = area_h_pt * 0.9
        fit_w = fit_h * aspect

    try:
        img = RLImage(str(logo_path), width=fit_w, height=fit_h)
        data[0][0] = img
    except Exception:
        # Best-effort: drop the logo rather than fail the export.
        pass
