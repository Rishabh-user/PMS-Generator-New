"""Resolve a (rating, material, corrosion allowance) selection to a §5.5
class code, and check whether that combination is in the catalogue extracted
from the reference Excel.

All the rules live in JSON:
  • app/data/class_naming.json    — rating letters, material/CA digits, suffix rules
  • app/data/class_catalogue.json — the 91 classes from the Excel INDEX sheet

So extending support to a new material is a JSON edit, not a code change.

Returned shape (see `resolve()`):
    {
        "class_code":  "A1",          # always — even when not catalogued
        "letter":      "A",
        "digit":       "1",
        "suffix":      "",
        "catalogued":  True,
        "catalogue_match":  {...}     # original row, when catalogued
        "catalogue_variants": [...]   # extra catalogued rows sharing the base code (T80 → T80A/B/C)
        "in_excel":    True,
        "note":        "...",
    }
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config import settings


class ResolutionError(ValueError):
    """The user's selection can't even be assembled into a §5.5 code."""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _naming_rules() -> dict:
    return json.loads((settings.data_dir / "class_naming.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _catalogue() -> list[dict]:
    raw = json.loads((settings.data_dir / "class_catalogue.json").read_text(encoding="utf-8"))
    return raw.get("classes", [])


def reload() -> None:
    """Drop cached JSON. Tests / re-extract calls clear via this so the
    next request re-reads the files without restarting the server."""
    _naming_rules.cache_clear()
    _catalogue.cache_clear()


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def _ca_token(ca: Optional[str]) -> str:
    """'3 mm' → '3'; 'NIL'/blank → 'NIL'; '1.5 mm' → '1.5'."""
    s = _norm(ca)
    if not s or s in ("NIL", "NONE", "0", "0 MM", "0MM"):
        return "NIL"
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return m.group(1) if m else "NIL"


def _strip_marker(text: str, marker: str) -> str:
    return re.sub(rf"\b{re.escape(marker)}\b", "", text).strip()


def _material_token(material: Optional[str]) -> tuple[str, bool, bool]:
    """Return (cleaned_material, is_nace, is_low_temp).

    'CS NACE'      → ('CS', True, False)
    'LTCS NACE'    → ('CS', True, True)         # LTCS implies low temp
    'CS GALV (Valve: SS)' → ('CS GALV', False, False)
    """
    rules = _naming_rules()["suffix_rules"]
    nace_marker = rules.get("nace_marker", "NACE").upper()
    low_marker  = rules.get("low_temp_marker", "LTCS").upper()

    raw = _norm(material)
    is_nace = nace_marker in raw
    is_low  = raw.startswith(low_marker)

    cleaned = _strip_marker(raw, nace_marker)
    if cleaned.startswith(low_marker):
        cleaned = "CS" + cleaned[len(low_marker):]
    cleaned = re.sub(r"\(.*?\)", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned, is_nace, is_low


# ---------------------------------------------------------------------------
# Core derivation (§5.5)
# ---------------------------------------------------------------------------

def derive_letter(rating: str) -> str:
    """'150#' → 'A'. Whitespace-tolerant, case-insensitive."""
    table = _naming_rules().get("rating_letters", {})
    target = _norm(rating).replace(" ", "")
    for label, letter in table.items():
        if _norm(label).replace(" ", "") == target:
            return letter
    raise ResolutionError(
        f"Unknown rating {rating!r}. Supported: {', '.join(table.keys())}"
    )


def derive_digit(material: str, ca: str) -> str:
    """(material, CA) → §5.5 digit. Both the rule's `material` field and
    the user input go through `_material_token` so they're compared on the
    same cleaned, paren-free form ('SS 316 / 316L (Tubing)' → 'SS 316 / 316L')."""
    base, _is_nace, _is_low = _material_token(material)
    ca_tok = _ca_token(ca)
    for rule in _naming_rules().get("material_digits", []):
        rule_base, _, _ = _material_token(rule["material"])
        if rule_base == base and _norm(rule["ca"]) == ca_tok:
            return rule["digit"]
    raise ResolutionError(
        f"No §5.5 digit defined for material={base!r}, CA={ca_tok!r}. "
        f"Add a rule to class_naming.json → material_digits."
    )


def derive_suffix(material: str, ca: str) -> str:
    """'L' for LTCS, 'N' for NACE, 'LN' for both. Includes the CS+6mm
    auto-NACE promotion rule the Excel uses."""
    rules = _naming_rules()["suffix_rules"]
    base, is_nace, is_low = _material_token(material)
    ca_tok = _ca_token(ca)

    for combo in rules.get("auto_nace_combos", []):
        combo_base, _, _ = _material_token(combo.get("material", ""))
        if combo_base == base and _norm(combo.get("ca", "")) == ca_tok:
            is_nace = True
            break

    suffix = ""
    if is_low:
        suffix += rules.get("low_temp_suffix", "L")
    if is_nace:
        suffix += rules.get("nace_suffix", "N")
    return suffix


def derive_class_code(rating: str, material: str, ca: str) -> dict:
    letter = derive_letter(rating)
    digit  = derive_digit(material, ca)
    suffix = derive_suffix(material, ca)
    return {
        "class_code": f"{letter}{digit}{suffix}",
        "letter":     letter,
        "digit":      digit,
        "suffix":     suffix,
    }


# ---------------------------------------------------------------------------
# Catalogue lookup
# ---------------------------------------------------------------------------

def _classes_by_code() -> dict[str, list[dict]]:
    """Group catalogue rows by uppercase class code so 'a1' lookups still hit."""
    out: dict[str, list[dict]] = {}
    for entry in _catalogue():
        out.setdefault(entry["class_code"].upper(), []).append(entry)
    return out


def _find_variants(base_code: str) -> list[dict]:
    """Catalogue rows whose code shares this base — used for tubings (T80 →
    T80A/B/C). Match is exact base + optional single trailing letter."""
    pattern = re.compile(rf"^{re.escape(base_code.upper())}[A-Z]?$")
    return [c for c in _catalogue() if pattern.match(c["class_code"].upper())]


def resolve(rating: str, material: str, ca: str, service: Optional[str] = None) -> dict:
    """Main entry point.

    Always returns a dict with `class_code` plus catalogue flags. Raises
    ResolutionError only when §5.5 itself can't accept the inputs (unknown
    rating or unknown material/CA pair)."""
    parts = derive_class_code(rating, material, ca)
    code  = parts["class_code"]
    code_upper = code.upper()

    by_code = _classes_by_code()
    exact = by_code.get(code_upper, [])
    variants: list[dict] = []
    if not exact:
        variants = _find_variants(code)

    catalogued   = bool(exact)
    has_variants = bool(variants) and not catalogued

    note = ""
    if catalogued:
        note = f"{code} exists in the Excel catalogue."
    elif has_variants:
        codes = ", ".join(sorted({v["class_code"] for v in variants}))
        note = (
            f"{code} is the §5.5 base code, but the Excel only lists tier "
            f"variants: {codes}. Pick a variant for an exact match, or "
            f"treat {code} as the family name."
        )
    else:
        note = (
            f"{code} is not in the current Excel catalogue — derived from §5.5 "
            f"naming rules. The combination is valid, just new."
        )

    return {
        "class_code":          code,
        "letter":              parts["letter"],
        "digit":               parts["digit"],
        "suffix":              parts["suffix"],
        "catalogued":          catalogued,
        "in_excel":            catalogued or has_variants,
        "catalogue_match":     exact[0] if exact else None,
        "catalogue_variants":  [v["class_code"] for v in variants],
        "service":             (service or "").strip(),
        "note":                note,
    }
