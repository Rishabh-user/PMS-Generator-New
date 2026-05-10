"""Allowable Stress S(T) lookup, ASME B31.3-2020 Table A-1.

Mirrors the deterministic recipe from the previous project:
  1. Pick the right stress table by regex match against material spec
  2. Linear-interpolate at the design temperature (clamp at endpoints)
  3. Round to nearest 100 psi (ASME convention)

All inputs come from `app/data/allowable_stress.json`. No AI, no formulas
beyond linear interpolation."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from app.config import settings


PSI_TO_MPA = 0.00689476
ROUND_PSI = 100


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((settings.data_dir / "allowable_stress.json").read_text(encoding="utf-8"))


def reload() -> None:
    _data.cache_clear()


def detect_table(material: str) -> Optional[str]:
    """Run the priority-ordered regex chain against `material` and return the
    first matching table key. Returns None when no rule matches (only
    happens if the catch-all default rule is removed)."""
    if not material:
        return None
    for rule in _data().get("spec_match_priority", []):
        if re.search(rule["pattern"], material):
            return rule["table"]
    return None


def lookup(material: str, temp_c: float) -> Optional[dict]:
    """Compute S at (material, temp_c).

    Returns:
        {
            "stress_psi": int (rounded to 100),
            "stress_mpa": float,
            "table_used": str (e.g. "CS"),
            "table_label": str,
            "interpolated": bool,
            "clamped": "low" | "high" | None,
            "source_pdf_page": int | None,
        }
    or None when:
      - material doesn't dispatch to any table
      - temp_c is None / NaN
      - the matched table has no data
    """
    if material is None or temp_c is None:
        return None

    table_key = detect_table(material)
    if not table_key:
        return None

    table = _data().get("tables", {}).get(table_key)
    if not table:
        return None

    stresses = table.get("stress_psi_by_temp_c") or {}
    if not stresses:
        return None

    keys_c = sorted(int(k) for k in stresses.keys())
    clamped: Optional[str] = None
    interpolated = False

    if temp_c <= keys_c[0]:
        s_psi = float(stresses[str(keys_c[0])])
        if temp_c < keys_c[0]:
            clamped = "low"
    elif temp_c >= keys_c[-1]:
        s_psi = float(stresses[str(keys_c[-1])])
        if temp_c > keys_c[-1]:
            clamped = "high"
    else:
        # Linear interpolate between adjacent breakpoints.
        s_psi = 0.0
        for i in range(len(keys_c) - 1):
            t1, t2 = keys_c[i], keys_c[i + 1]
            if t1 <= temp_c <= t2:
                s1 = float(stresses[str(t1)])
                s2 = float(stresses[str(t2)])
                frac = (temp_c - t1) / (t2 - t1)
                s_psi = s1 + frac * (s2 - s1)
                interpolated = True
                break

    rounded = int(round(s_psi / ROUND_PSI) * ROUND_PSI)

    return {
        "stress_psi":      rounded,
        "stress_mpa":      round(rounded * PSI_TO_MPA, 1),
        "table_used":      table_key,
        "table_label":     table.get("label", table_key),
        "interpolated":    interpolated,
        "clamped":         clamped,
        "source_pdf_page": table.get("source_pdf_page"),
    }
