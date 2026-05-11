"""Branch Connection Chart selection per material family.

Project Appendix-1 lists four authoritative charts:
  • Chart-1  CS / LTCS / SS / DSS / SDSS     (T / W)
  • Chart-2  CS GALV                         (H / W / T)
  • Chart-3  CuNi                            (S / W / T)
  • Chart-4  GRE                             (T / RT / S / -)

The matrices live in `app/data/branch_charts.json` (project data — would
need an engineering revision to change). This module just picks the
right chart for a material and returns the full payload (axis + matrix
+ legend + title) for the frontend / Excel exporter to render.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

from app.config import settings
from app.services import fitting_specs


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((settings.data_dir / "branch_charts.json").read_text(encoding="utf-8"))


def reload() -> None:
    _data.cache_clear()


# ---------------------------------------------------------------------------
# Family → chart mapping
# ---------------------------------------------------------------------------
# Use the `family` field returned by fitting_specs.lookup() — that's the
# canonical material grouping already used elsewhere in code_factors.
_FAMILY_TO_CHART: dict[str, str] = {
    # Chart 1 — carbon, low-temp, stainless, duplex, super-duplex, exotics
    "Carbon Steel":         "chart_1",
    "CS NACE":              "chart_1",
    "LTCS":                 "chart_1",
    "LTCS NACE":            "chart_1",
    "SS316L":               "chart_1",
    "SS316L NACE":          "chart_1",
    "Duplex (S31803)":      "chart_1",
    "Super Duplex (S32750)":"chart_1",
    "Titanium Gr 2":        "chart_1",
    "Copper":               "chart_1",
    "6 MO Tubing":          "chart_1",
    "SS 316 / 316L Tubing": "chart_1",
    "Epoxy-Lined CS":       "chart_1",

    # Chart 2 — galvanised carbon steel
    "Galvanised CS":        "chart_2",

    # Chart 3 — 90/10 CuNi
    "90/10 CuNi":           "chart_3",

    # Chart 4 — non-metallic (GRE + CPVC default to saddle/RT pattern)
    "Glass-Reinforced Epoxy": "chart_4",
    "CPVC":                   "chart_4",
}


def pick_chart_id(material: str) -> Optional[str]:
    """Resolve the material to its Appendix-1 chart id (or None)."""
    spec = fitting_specs.lookup(material)
    if not spec:
        return None
    return _FAMILY_TO_CHART.get(spec.get("family"))


def build(material: str) -> Optional[dict]:
    """Return the chart payload for this material, or None when the
    material has no project-listed branch chart (rare — most fall under
    Chart-1)."""
    chart_id = pick_chart_id(material)
    if not chart_id:
        return None
    chart = _data().get("charts", {}).get(chart_id)
    if not chart:
        return None
    # Echo the resolved family so the frontend can show "for: Carbon Steel"
    spec = fitting_specs.lookup(material) or {}
    return {
        **chart,
        "resolved_family": spec.get("family"),
    }
