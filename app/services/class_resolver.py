"""Resolve a (rating, material, corrosion allowance) selection to a §5.5
class code. Pure derivation — every catalogued class in the project Excel
follows the same naming convention, so the rules in `class_naming.json`
cover both catalogued and brand-new combinations identically.

All rules live in JSON:
  • app/data/class_naming.json — rating letters, material/CA digits, suffix rules

Returned shape (see `resolve()`):
    {
        "class_code":  "A1",
        "letter":      "A",
        "digit":       "1",
        "suffix":      "",
        "service":     "",
        "note":        "Derived from §5.5 rules.",
    }
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from app.config import settings
from app.services import pt_lookup, stress_lookup, y_lookup, fitting_specs, flange_specs, branch_chart


class ResolutionError(ValueError):
    """The user's selection can't be assembled into a §5.5 code."""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _naming_rules() -> dict:
    return json.loads((settings.data_dir / "class_naming.json").read_text(encoding="utf-8"))


def reload() -> None:
    """Drop cached JSON. Tests / re-extract calls clear via this so the
    next request re-reads the file without restarting the server."""
    _naming_rules.cache_clear()


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


def _valid_cas_for_material(base: str) -> list[str]:
    """Return the corrosion-allowance values catalogued for this
    cleaned material token, in display order. Used to enrich the
    'no digit defined' error so the user sees concrete next steps
    instead of an internal-doc reference."""
    rules = _naming_rules().get("material_digits", []) or []
    found: list[str] = []
    for rule in rules:
        rule_base, _, _ = _material_token(rule.get("material") or "")
        if rule_base == base:
            ca = (rule.get("ca") or "").strip()
            display = "NIL" if _norm(ca) == "NIL" else f"{ca} mm"
            if display not in found:
                found.append(display)
    return found


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

    # The (material, CA) pair has no catalogued class. Surface a
    # user-friendly error: name the material, name what corrosion
    # allowance(s) it CAN take, and skip the internal-doc reference
    # (§5.5 / class_naming.json) that only an operator can act on.
    ca_display = "NIL" if ca_tok == "NIL" else f"{ca_tok} mm"
    valid_cas = _valid_cas_for_material(base)
    if valid_cas:
        if len(valid_cas) == 1:
            options = valid_cas[0]
        elif len(valid_cas) == 2:
            options = f"{valid_cas[0]} or {valid_cas[1]}"
        else:
            options = ", ".join(valid_cas[:-1]) + f", or {valid_cas[-1]}"
        raise ResolutionError(
            f"{material} doesn't use a {ca_display} corrosion allowance. "
            f"Pick {options} to match the catalogued PMS classes for "
            f"this material."
        )
    raise ResolutionError(
        f"{material} isn't a catalogued PMS material yet. Pick a "
        f"different material from the dropdown — or ask the project "
        f"engineer to add it to the catalogue."
    )


def derive_suffix(material: str, ca: str) -> str:
    """'L' for LTCS, 'N' for NACE, 'LN' for both. Includes the CS+6mm
    auto-NACE promotion rule the project Excel uses."""
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


def _tubing_variant(rating: str) -> str:
    """Tubing pressure tiers — 'Tubing A' / 'Tubing B' / 'Tubing C' append
    A / B / C to the class code (e.g. T80A, T80B, T80C). The plain
    'Tubing' rating still produces T80 / T90 with no trailing variant."""
    if not rating:
        return ""
    m = re.match(r"^\s*Tubing\s+([A-Z])\s*$", rating, re.I)
    return m.group(1).upper() if m else ""


def _effective_material(material: str, ca: str) -> str:
    """Apply auto-NACE promotion BEFORE downstream lookups so the
    stress curve, pipe MOC, fitting specs, and valve NACE-trim notes
    all agree with the class code's N suffix.

    Example bug this prevents:
      Input  → rating=2500#, material='CS',      ca='6 mm'
      Class  → G2N           (N suffix auto-applied for CS + 6mm)
      Before fix:
        stress = 20,000 psi (A106 Gr B table)   ← inconsistent
        pipe   = ASTM A 106 Gr. B / API 5L Gr. B ← inconsistent
      After fix:
        stress = 25,000 psi (X60 PSL-2 table)   ← matches G2N
        pipe   = API 5L Gr. X60 PSL-2            ← matches G2N

    Without this, the same class code (G2N) would produce two
    different engineering snapshots depending on whether the user
    typed 'CS' or 'CS NACE' — even though the class code says the
    SAME thing in both cases.

    Rule source: `class_naming.json → suffix_rules.auto_nace_combos`.
    Currently only `CS + 6 mm` is auto-promoted; the helper supports
    additional combos in that JSON list automatically."""
    rules = _naming_rules().get("suffix_rules") or {}
    base, is_nace, is_low = _material_token(material)
    if is_nace:
        return material  # already explicit — no promotion needed
    ca_tok = _ca_token(ca)
    for combo in rules.get("auto_nace_combos", []):
        combo_base, _, _ = _material_token(combo.get("material", ""))
        if combo_base == base and _norm(combo.get("ca", "")) == ca_tok:
            # Promote — preserve the LTCS marker if the input was LTCS.
            return "LTCS NACE" if is_low else "CS NACE"
    return material


_GRE_MATERIAL_RE      = re.compile(r"(?i)\bGRE\b|EPOXY\s*FIBRE|Glass.*Reinforced")
_GRE_HYPOCHLORITE_RE  = re.compile(r"(?i)Hypochlorite|BONSTRAND")
_GRE_SPECIAL_RE       = re.compile(r"(?i)\bSpecial\b")


def _service_digit_override(material: str, service: Optional[str]) -> Optional[str]:
    """Project rule: GRE classes split by service.
        Raw Sea Water / Topside Seawater (default)  → digit 50 (class A50)
        Hypochlorite  / BONSTRAND-bound services    → digit 51 (class A51)
        "Special Services" / similar wording        → digit 52 (class A52)

    Other materials don't have service-dependent digits — return None and
    let derive_digit() use the catalog default."""
    if not material or not _GRE_MATERIAL_RE.search(material):
        return None
    s = service or ""
    if _GRE_HYPOCHLORITE_RE.search(s):
        return "51"
    if _GRE_SPECIAL_RE.search(s):
        return "52"
    return None  # default to the catalog rule → 50


def _check_rating_restrictions(letter: str, digit: str,
                               rating: str, material: str) -> None:
    """Enforce two complementary (rating, material) restrictions from
    `class_naming.json` → `rating_restrictions`:

    1. `low_pressure_only_digits` — material-digits catalogued ONLY at
       low-pressure ratings (150# / EEMUA 20 bar):
         • 30 / 40   — CuNi / Copper (physical pressure limit)
         • 50 / 51 / 52 — GRE variants (composite, low-pressure only)
         • 60        — CPVC (plastic, low-pressure only)
         • 70        — Titanium (project policy — Ti only at 150#)

    2. `tubing_only_digits` — when the rating is an instrument-tubing
       series (letter `T` = Tubing A/B/C), only these material digits
       are valid (carbon steel / SS plate grades / duplex etc. don't
       exist as instrument-tubing products):
         • 80 — SS 316 / 316L (Tubing)
         • 90 — 6 MO Tubing

    Raises ResolutionError when either rule is violated. Callers that
    enumerate combos (chat agent's `_safe_resolve`) catch this and
    skip silently; /api/resolve-class and /api/compute-pms surface it
    as a 422.
    """
    rules = _naming_rules().get("rating_restrictions") or {}
    rating_letters = _naming_rules().get("rating_letters") or {}

    # ── Rule 1: material restricted to low-pressure rating(s) ──
    restricted = set(rules.get("low_pressure_only_digits") or [])
    if digit in restricted:
        allowed_letter = rules.get("low_pressure_only_letter", "A")
        if letter != allowed_letter:
            allowed_ratings = [r for r, lt in rating_letters.items() if lt == allowed_letter]
            raise ResolutionError(
                f"Material {material!r} is only catalogued at low-pressure "
                f"ratings ({', '.join(allowed_ratings) or allowed_letter}). "
                f"The requested rating {rating!r} doesn't apply to this "
                f"material — pick a different rating or a different material."
            )

    # ── Rule 2: tubing rating restricted to tubing-material digits ──
    tubing_letter = rules.get("tubing_letter")
    tubing_only_digits = set(rules.get("tubing_only_digits") or [])
    if tubing_letter and tubing_only_digits and letter == tubing_letter:
        if digit not in tubing_only_digits:
            tubing_ratings = [r for r, lt in rating_letters.items() if lt == tubing_letter]
            raise ResolutionError(
                f"Rating {rating!r} is an instrument-tubing series — "
                f"only tubing materials are valid here (SS 316 / 316L (Tubing), "
                f"6 MO Tubing). The material {material!r} doesn't exist as a "
                f"tubing product. Pick a tubing material — or pick a flange "
                f"rating ({', '.join(r for r in rating_letters if r not in tubing_ratings)[:60]}…) "
                f"if you actually want {material} piping."
            )


def derive_class_code(rating: str, material: str, ca: str,
                      service: Optional[str] = None) -> dict:
    letter   = derive_letter(rating)
    base_digit = derive_digit(material, ca)
    # Service-aware overrides — currently only used for GRE A50/A51/A52.
    override = _service_digit_override(material, service)
    digit = override or base_digit
    suffix   = derive_suffix(material, ca)
    trailing = _tubing_variant(rating)

    # Project rule: reject (rating, material) combos that don't exist in
    # real catalogues — e.g. Copper @ 300#, Titanium @ 600#. Keeps the
    # SPA from generating a nonsense PMS and keeps chat-agent matches
    # to physically realistic classes only.
    _check_rating_restrictions(letter, digit, rating, material)

    return {
        "class_code": f"{letter}{digit}{trailing}{suffix}",
        "letter":     letter,
        "digit":      digit,
        "suffix":     suffix,
        "trailing":   trailing,
    }


def resolve(rating: str, material: str, ca: str, service: Optional[str] = None) -> dict:
    """Main entry point.

    Returns the §5.5 class code, the matching ASME B16.5 P-T table (when
    one is indexed for this rating/material), and the per-material code
    factor tables (allowable stress S vs T, Y coefficient vs T) so the
    frontend can interpolate live as the user edits design conditions on
    Tab 2 / Tab 3 without another round trip.

    Raises ResolutionError when the inputs don't fit the §5.5 rules
    (unknown rating, or unknown material/CA pair — extend
    `class_naming.json` to fix)."""
    parts = derive_class_code(rating, material, ca, service)
    # Auto-NACE-promotion: if the (material, CA) combo triggers a NACE
    # auto-suffix on the class code, all downstream lookups (stress
    # curve, pipe MOC, fitting specs, valve NACE-trim notes) need to
    # see the promoted material — otherwise the snapshot reports a
    # class like G2N but uses the pre-promotion CS stress curve
    # (20,000 psi A106) instead of the NACE X60 curve (25,000 psi
    # at 1500#/2500#). The promoted string is internal only — the
    # user's typed material is still echoed back via the SPA form.
    effective_material = _effective_material(material, ca)
    # `service` is passed through so pt_lookup can swap in service-bound
    # alternates (e.g. GRE + Hypochlorite → BONSTRAND 50000C curve).
    pt    = pt_lookup.find(rating, effective_material, service=service)
    code_factors = _build_code_factors(
        effective_material, pt, rating, parts["class_code"], service,
    )

    return {
        **parts,
        "service":              (service or "").strip(),
        "note":                 f"Class {parts['class_code']} derived from §5.5 naming rules.",
        "pressure_temperature": pt,
        "code_factors":         code_factors,
    }


def _build_code_factors(material: str, pt: Optional[dict], rating: Optional[str] = None,
                        class_code: Optional[str] = None,
                        service: Optional[str] = None) -> dict:
    """Bundle the stress-table row and Y-curve row that apply to this
    material so the frontend can do live S(T) and Y(T) lookups.

    `rating` is passed so project pipe-grade promotions apply — e.g. a
    1500# CS NACE class routes to API 5L X60, not A106 Gr B.

    Computes the cold-end S (S₁) immediately so the report card has
    something to render before the user edits the design temperature."""
    table_key = stress_lookup.detect_table(material, rating)
    stress_table = stress_lookup._data().get("tables", {}).get(table_key) if table_key else None  # noqa: SLF001
    y_category   = y_lookup.detect_category(material)
    y_block      = y_lookup._data().get("materials", {}).get(y_category)  # noqa: SLF001

    # Cold-rated point (lowest indexed temp from the P-T envelope) is what
    # the Excel uses as Case 1 — surface the S there so the GOVERNS marker
    # has a real number, not a placeholder.
    cold_t_c = None
    if pt and pt.get("temperatures_c"):
        cold_t_c = min(pt["temperatures_c"])
    s_cold = stress_lookup.lookup(material, cold_t_c, rating) if cold_t_c is not None else None

    return {
        "stress_table": stress_table and {
            "key":              table_key,
            "label":            stress_table.get("label"),
            "stress_psi_by_temp_c": stress_table.get("stress_psi_by_temp_c"),
            "max_temp_c":       stress_table.get("max_temp_c"),
            "source_pdf_page":  stress_table.get("source_pdf_page"),
        },
        "y_curve": y_block and {
            "category":      y_category,
            "label":         y_block.get("label"),
            "temperatures_c": y_lookup._data().get("temperatures_c"),  # noqa: SLF001
            "y_values":       y_block.get("y_values"),
        },
        "cold_temp_c":     cold_t_c,
        "stress_at_cold":  s_cold,
        "fitting_specs":   fitting_specs.lookup(material, rating),
        "flange_extras":   flange_specs.build(
            rating, material,
            (fitting_specs.lookup(material, rating) or {}).get("flange"),
            class_code,
            service,
        ),
        "branch_chart":    branch_chart.build(material),
    }
