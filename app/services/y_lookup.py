"""Coefficient Y lookup, ASME B31.3-2020 Table 304.1.1.

Y is used in the wall-thickness equation:  t = PD / [2(SE·W + P·Y)]

The standard tabulates Y per material category × temperature. Below 482°C
all four metallic categories share Y = 0.4; above that Y rises in steps to
0.7 depending on category. We do linear interpolation between published
breakpoints to handle intermediate design temps.

The material → category mapping is project-specific — austenitic stainless
goes to one curve, ferritic / duplex / non-ferrous metals to another. The
mapping below lives next to the lookup so a new material added to Step 1's
dropdowns gets a single-line edit here, not a JSON change."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from app.config import settings


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((settings.data_dir / "y_coefficient.json").read_text(encoding="utf-8"))


def reload() -> None:
    _data.cache_clear()


# Project-material → B31.3 Y-curve mapping. Order matters.
_CATEGORY_RULES = [
    (re.compile(r"(?i)\bSDSS\b|S32750|SUPER\s*DUPLEX"),         "ferritic_steels"),  # duplex shares ferritic curve up to high temp
    (re.compile(r"(?i)\bDSS\b|S31803|S32205|\bDUPLEX\b"),       "ferritic_steels"),
    (re.compile(r"(?i)TP\s*316L|TP\s*304L|TP\s*316|316L|304L|316|\bSS\b|STAINLESS|6\s*MO|N08367"), "austenitic_steels"),
    (re.compile(r"(?i)\bTITANIUM\b|\bB\s*861\b"),               "other_ductile_metals"),
    (re.compile(r"(?i)\bCOPPER\b|\bC12200\b|\bB\s*42\b|\bCuNi\b|C70600|\bB\s*466\b"), "other_ductile_metals"),
    (re.compile(r"(?i)\bNICKEL\b|N06617|N08800|N08810|N08825"), "nickel_alloys"),
    # Default: ferritic (covers CS, LTCS, NACE, GALV, A106, A234, A333, etc.)
    (re.compile(r".*"),                                          "ferritic_steels"),
]


def detect_category(material: str) -> str:
    if not material:
        return "ferritic_steels"
    for rx, cat in _CATEGORY_RULES:
        if rx.search(material):
            return cat
    return "ferritic_steels"


def lookup(material: str, temp_c: float) -> Optional[dict]:
    """Compute Y at (material, temp_c).

    Returns:
        {
            "y":            float,
            "category":     "ferritic_steels" | "austenitic_steels" | "nickel_alloys" | "other_ductile_metals",
            "category_label": str,
            "interpolated": bool,
            "clamped":      "low" | "high" | None,
        }
    """
    if temp_c is None:
        return None

    data = _data()
    category = detect_category(material)
    cat_block = data.get("materials", {}).get(category)
    if not cat_block:
        return None

    temps_c = data.get("temperatures_c") or []
    y_vals  = cat_block.get("y_values") or []
    if not temps_c or not y_vals:
        return None

    # Drop trailing None entries (gray iron only has one value).
    pairs = [(t, y) for t, y in zip(temps_c, y_vals) if y is not None]
    if not pairs:
        return None

    clamped: Optional[str] = None
    interpolated = False

    if temp_c <= pairs[0][0]:
        y = pairs[0][1]
        if temp_c < pairs[0][0]:
            clamped = "low"
    elif temp_c >= pairs[-1][0]:
        y = pairs[-1][1]
        if temp_c > pairs[-1][0]:
            clamped = "high"
    else:
        y = pairs[-1][1]
        for i in range(len(pairs) - 1):
            t1, y1 = pairs[i]
            t2, y2 = pairs[i + 1]
            if t1 <= temp_c <= t2:
                frac = (temp_c - t1) / (t2 - t1)
                y = y1 + frac * (y2 - y1)
                interpolated = True
                break

    return {
        "y":              round(y, 2),
        "category":       category,
        "category_label": cat_block.get("label", category),
        "interpolated":   interpolated,
        "clamped":        clamped,
    }
