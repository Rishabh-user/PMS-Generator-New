"""POST /api/export/pdf — stream a PDF rendering of the PMS datasheet.

Implementation strategy: reuse `excel_exporter.build_workbook()` to produce
the canonical .xlsx (single source of truth for the datasheet layout),
then convert that .xlsx to PDF by shelling out to LibreOffice's headless
mode (`soffice --headless --convert-to pdf`).

Why LibreOffice?
  • The Excel exporter is already the layout source of truth — drawing a
    second native PDF (e.g. with reportlab) would mean re-implementing
    every merge / fill / border rule and the two outputs would inevitably
    drift apart.
  • LibreOffice renders the .xlsx exactly as Excel would, so the PDF the
    user sees matches the Excel they download bit-for-bit visually.

Deployment requirements:
  • Dev box (Windows): install LibreOffice from libreoffice.org. The
    locator below covers the standard install paths.
  • Linux / Render: install via apt — add `libreoffice` to `apt.txt` in
    your Render service so the buildpack pulls it in. Alternatively, a
    custom Dockerfile with `RUN apt-get install -y libreoffice`.

If LibreOffice isn't present, `find_libreoffice()` returns None and the
caller surfaces a 503 with an install hint — the rest of the app
continues to work, only the PDF endpoint degrades.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from app.services import excel_exporter


# ── LibreOffice locator ─────────────────────────────────────────────

# Standard install paths we probe in order. The first executable that
# exists wins. None means LibreOffice isn't installed on this host.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    r"C:\Program Files\LibreOffice 7\program\soffice.exe",
    r"C:\Program Files\LibreOffice 24\program\soffice.exe",
    r"C:\Program Files\LibreOffice 25\program\soffice.exe",
)
_POSIX_CANDIDATES = (
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/usr/local/bin/libreoffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def find_libreoffice() -> Optional[str]:
    """Return the absolute path to the LibreOffice executable, or None
    if it isn't installed on this host. Checks $PATH first, then the
    standard install locations for the current OS."""
    # PATH lookup catches custom installs.
    for name in ("soffice", "libreoffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found

    candidates = _WINDOWS_CANDIDATES if sys.platform.startswith("win") else _POSIX_CANDIDATES
    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


class LibreOfficeMissingError(RuntimeError):
    """Raised when no LibreOffice binary can be located on the host."""


class LibreOfficeConversionError(RuntimeError):
    """Raised when the XLSX→PDF conversion fails or produces no output."""


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
    """Generate a PDF of the PMS datasheet for the resolved class.

    Returns (BytesIO, filename) — same shape as `excel_exporter.build_workbook`
    so the route layer can stream it identically.

    Raises:
      LibreOfficeMissingError      — soffice not installed on this host.
      LibreOfficeConversionError   — conversion ran but produced no PDF.
      class_resolver.ResolutionError — class lookup failed (propagates from
                                       excel_exporter.build_workbook).
    """
    soffice = find_libreoffice()
    if not soffice:
        raise LibreOfficeMissingError(
            "LibreOffice is not installed on this server, so PDF export is "
            "unavailable. Install it from libreoffice.org (Windows / macOS) "
            "or `apt-get install -y libreoffice` (Linux / Render) and "
            "restart the API."
        )

    # 1. Build the canonical Excel — same code path as /api/export/excel.
    xlsx_buf, xlsx_filename = excel_exporter.build_workbook(
        rating=rating,
        material=material,
        ca=ca,
        service=service,
        design_p_barg=design_p_barg,
        design_t_c=design_t_c,
        mdmt_c=mdmt_c,
        joint_type=joint_type,
    )

    # 2. Stage the XLSX in a fresh tempdir, convert, read back the PDF.
    #
    # We use a per-conversion tempdir (mkdtemp) so concurrent requests
    # don't race on output filenames — LibreOffice writes `<stem>.pdf`
    # next to its input, so two simultaneous conversions of "PMS-A1.xlsx"
    # would otherwise overwrite each other.
    tmpdir = Path(tempfile.mkdtemp(prefix="pms_pdf_"))
    try:
        xlsx_path = tmpdir / xlsx_filename
        xlsx_path.write_bytes(xlsx_buf.getvalue())

        # --headless     no GUI
        # --norestore    don't try to recover the last session
        # --nolockcheck  skip the .~lock check (we own this tempdir)
        # -env:UserInstallation  point soffice's per-user config dir at
        #                a tempdir we own. Default is $HOME/.config/
        #                libreoffice which (on locked-down hosts like
        #                Render's runtime user) isn't writable; without
        #                this override soffice can fail to start or hang
        #                on first run while trying to write its profile.
        # --convert-to pdf  the conversion target
        # --outdir       write the PDF here
        user_profile_dir = tmpdir / "lo_profile"
        result = subprocess.run(
            [
                soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                f"-env:UserInstallation=file://{user_profile_dir}",
                "--convert-to", "pdf",
                "--outdir", str(tmpdir),
                str(xlsx_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise LibreOfficeConversionError(
                "LibreOffice returned exit code "
                f"{result.returncode}. stderr: {result.stderr.strip()[:500]}"
            )

        # LibreOffice names the output `<input-stem>.pdf` in --outdir.
        pdf_stem = Path(xlsx_filename).stem
        pdf_path = tmpdir / f"{pdf_stem}.pdf"
        if not pdf_path.exists():
            raise LibreOfficeConversionError(
                f"LibreOffice reported success but no PDF was produced. "
                f"stderr: {result.stderr.strip()[:500]}"
            )

        pdf_bytes = pdf_path.read_bytes()
    finally:
        # Best-effort cleanup. ignore_errors so a transient AV / file-lock
        # never bubbles up as a failed conversion when the PDF is already
        # in our hands.
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 3. Filename — same sanitisation rule as Excel, but with .pdf extension.
    pdf_filename = re.sub(r"\.xlsx$", ".pdf", xlsx_filename)
    return io.BytesIO(pdf_bytes), pdf_filename
