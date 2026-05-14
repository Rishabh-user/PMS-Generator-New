"""PMS-Agent chat — natural-language slot filling on top of the new
class_resolver / options catalog.

Front-end contract (kept backward-compatible with the PMSAgentPage UI):

    Request:
        { prompt: str, history: [{role, content}, ...] }

    Response:
        {
            reply, interpreted, matched_classes, suggested_action,
            slots: { rating, material, corrosion_allowance, service,
                     missing, complete,
                     # multi-value extensions (additive, optional):
                     ratings, materials, corrosion_allowances, services,
                     exclusions, rating_min, rating_max },
            field_suggestions, available_values, allow_bulk_download,
        }

How it works:
  1. Claude extracts ARRAYS of slot values + exclusion flags + numeric
     range from the user's prompt + history (multi-value friendly).
  2. Each value is validated against the catalog; mis-spellings produce
     did-you-mean suggestions.
  3. Filters are applied: rating range narrows the rating set, exclusion
     flags remove NACE / LTCS variants from materials.
  4. Enumeration: the cartesian product of filtered ratings × materials ×
     CAs is resolved to class codes. Empty filters mean "all" for that
     dimension. Cap defaults to 60; ranked by score so the most relevant
     show first.
  5. ANY filter (even just one slot) triggers enumeration — the old
     "need 2+ slots" gate is gone. With zero filters we still ask for
     more info instead of dumping the entire catalog.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from functools import lru_cache
from typing import Any, Optional

from app.config import settings
from app.services import ai_service, class_resolver, pms_snapshot, pt_lookup


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Catalog loaders
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _catalog() -> dict:
    data_dir = settings.data_dir

    def load(name: str) -> Any:
        return json.loads((data_dir / name).read_text(encoding="utf-8"))

    return {
        "ratings":             load("pressure_ratings.json").get("ratings", []),
        "materials":           load("materials.json").get("materials", []),
        "corrosion_allowances": load("corrosion_allowances.json").get("corrosion_allowances", []),
        "services":            load("services.json").get("services", []),
    }


def reload_catalog() -> None:
    _catalog.cache_clear()


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _match_catalog(value: str, candidates: list[str]) -> Optional[str]:
    if not value:
        return None
    target = _norm(value)
    for c in candidates:
        if _norm(c) == target:
            return c
    return None


def _suggest(value: str, candidates: list[str], n: int = 3) -> list[str]:
    if not value:
        return []
    target = _norm(value)
    scored: list[tuple[float, str]] = []
    for c in candidates:
        ratio = difflib.SequenceMatcher(None, target, _norm(c)).ratio()
        if ratio > 0.45 or target in _norm(c) or _norm(c) in target:
            scored.append((ratio, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [c for _r, c in scored[:n]]


def _rating_to_num(rating: str) -> Optional[float]:
    """'150#' → 150, '1500#' → 1500, 'Tubing' → None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*#?\s*$", rating or "")
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
You are the natural-language router for a Piping Material Specification
(PMS) chat assistant. Read the user's latest message PLUS the full
conversation history and extract structured filters.

Output STRICT JSON with this schema (no markdown, no preamble, no prose):

{
  "ratings":              ["<canonical rating>", ...]   // see CATALOG below
  "materials":            ["<canonical material>", ...] // see CATALOG below
  "corrosion_allowances": ["<canonical CA>", ...]       // NIL | 1.5 mm | 3 mm | 6 mm
  "services":             ["<canonical service>", ...]  // see CATALOG below; multi-OK
  "exclude_nace":         <bool>     // true if user says "no NACE" / "exclude NACE" / "non-sour"
  "exclude_low_temp":     <bool>     // true if user says "no LTCS" / "exclude low temperature"
  "rating_min":           <number|null>  // psi value (e.g. 900) for "above 900"
  "rating_min_inclusive": <bool>          // true for "≥ 900" / "at least 900"; false for "above 900"
  "rating_max":           <number|null>
  "rating_max_inclusive": <bool>
  "design_temp_c":        <number|null>
  "design_pressure_barg": <number|null>
  "intent":               "generate" | "list" | "info" | "unknown"
  "reply":                "<one short conversational sentence>"
}

# CATALOG
Ratings: 150#, 300#, 600#, 900#, 1500#, 2500#, 5000#, 10000#, EEMUA 20 bar,
         Tubing, Tubing A, Tubing B, Tubing C

Materials: CS, CS NACE, LTCS, LTCS NACE, CS GALV (Valve: SS), CS GALV,
           CS - Epoxy Lined, SS316L, SS316L NACE, DSS, DSS NACE, SDSS,
           SDSS NACE, CuNi (Valve: NAB), Copper, GRE (Valve: NAB),
           CPVC (Valve: NAB), TITANIUM, SS 316 / 316L (Tubing), 6 MO Tubing

Corrosion Allowances: NIL, 1.5 mm, 3 mm, 6 mm

Services (sample — pass through free text if not exact):
  Cooling Media, Heating Media, Diesel, Steam, Water Injection, Fresh Water,
  Hydraulic Oil, Nitrogen, Exhaust, Fuel Oil, Tank Air Vent, Glycol, FG,
  Hydro Carbon service, Corrosive Hydro Carbon service, Flare,
  Hydro Carbon service (Low Temp), Gas Lift, Utility Water, Bilge, Sewage,
  Seawater, Firewater, Air, Lube oil, Chemical, Foam, Instrument Air,
  Diesel Fuel, Raw Sea Water, Potable Water, Hypochlorite,
  Chemical (Ferric chloride), Coagulant, Chemical Injection (Except Hypochlorite)

# RULES
1. ARRAYS: every slot is an array. Single value → 1-element array. None → [].
2. CANONICALISE aggressively when there's an obvious match:
     "150 lb" / "150lb" / "class 150"   → ["150#"]
     "carbon steel"                     → ["CS"]
     "stainless 316"                    → ["SS316L"]
     "duplex" / "duplex stainless"      → ["DSS"]
     "super duplex"                     → ["SDSS"]
     "sour" / "NACE" (as a material qualifier with CS) → ["CS NACE"]
     "low temperature carbon steel"     → ["LTCS"]
     "3mm" / "three mm"                 → ["3 mm"]
     "no CA" / "nil"                    → ["NIL"]
     "hydraulic oil"                    → ["Hydraulic Oil"]
     "corrosive hydrocarbon"            → ["Corrosive Hydro Carbon service"]
3. MULTI-VALUE within a field is OR:
     "DSS or SDSS"                      → materials: ["DSS", "SDSS"]
     "Glycol, FG, Hydro Carbon service" → services: ["Glycol", "FG", "Hydro Carbon service"]
4. CROSS-FIELD is AND. The match must satisfy every field's filter.
5. CONCEPTUAL MAPPING — expand semantic descriptors to concrete catalog values:
     "corrosion-resistant material"     → ["SS316L", "SS316L NACE", "DSS", "DSS NACE", "SDSS", "SDSS NACE"]
     "sour service" / "NACE compliance" → mark exclude_nace=false AND filter materials to NACE variants only when context allows
     "low temperature hydrocarbon"      → materials: ["LTCS", "LTCS NACE"], services: ["Hydro Carbon service (Low Temp)"]
     "seawater handling"                → materials: ["CuNi (Valve: NAB)", "Copper", "SS316L", "GRE (Valve: NAB)"]
6. EXCLUSION:
     "no NACE" / "exclude NACE" / "non-sour"     → exclude_nace=true
     "no LTCS" / "exclude low temperature"       → exclude_low_temp=true
7. NUMERIC RANGE on rating:
     "above 900"          → rating_min=900, rating_min_inclusive=false
     "≥ 600" / "at least 600" → rating_min=600, rating_min_inclusive=true
     "below 1500"         → rating_max=1500, rating_max_inclusive=false
     "between 300 and 900" → rating_min=300, rating_max=900, both inclusive
8. NACE COMPLIANCE: when the user asks for "NACE compliance" without
   naming a specific material, populate materials with the NACE variants:
     "NACE compliance"                  → ["CS NACE", "LTCS NACE", "SS316L NACE", "DSS NACE", "SDSS NACE"]
   When the user asks for a specific material WITH "NACE", canonicalise
   to that specific NACE variant (e.g. "DSS with NACE" → ["DSS NACE"]).
9. DESIGN PRESSURE / TEMPERATURE ARE NOT THE RATING:
   `design_pressure_barg` and `design_temp_c` are operating-point inputs
   (what the line must sustain). The flange `rating` is a fixed spec
   (150# / 300# / 600# / …). DO NOT infer or fill `ratings`,
   `rating_min`, or `rating_max` from a design pressure / temperature.
   Examples:
     "generate pms for 532 barg at 400°C"
        → design_pressure_barg=532, design_temp_c=400,
          ratings=[], rating_min=null, rating_max=null
          (the backend will search the catalog for classes that can sustain it)
     "200 barg at 250°C"
        → design_pressure_barg=200, design_temp_c=250, ratings=[]
   Only populate `ratings` when the user names a rating directly
   ("150#", "class 300", "class 600 flange"). Phrases like
   "operating pressure 200 barg" / "design at X barg" / "needs to hold X
   bar at Y°C" all belong in design_pressure_barg, never in ratings.
10. Numbers are JSON numbers, not strings. Booleans default to false.
11. Output JSON ONLY. No code fences, no commentary.
"""


def _extract_filters_with_claude(prompt: str, history: list[dict]) -> dict:
    """Call Claude to extract structured filters. Falls back to a stub
    when the key isn't configured or the call fails.

    The returned dict carries an extra `_meta` key with Claude token
    usage + per-call latency so the route can log them to
    `pms_agent_queries` for cost / perf monitoring. The route strips
    `_meta` before returning to the SPA."""
    if not ai_service.is_available():
        empty = _empty_extraction(
            "AI is not configured on this server. Ask the operator to set "
            "ANTHROPIC_API_KEY in the backend .env."
        )
        empty["_meta"] = {"model": None, "tokens_in": None, "tokens_out": None, "claude_ms": 0}
        return empty

    client = ai_service._client_or_none()  # noqa: SLF001
    if client is None:
        empty = _empty_extraction("AI client unavailable; check ANTHROPIC_API_KEY.")
        empty["_meta"] = {"model": None, "tokens_in": None, "tokens_out": None, "claude_ms": 0}
        return empty

    messages: list[dict] = []
    for turn in (history or []):
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": prompt})

    import time as _time
    t0 = _time.perf_counter()
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=900,
            system=[{
                "type": "text",
                "text": _EXTRACT_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Claude slot-extraction failed")
        empty = _empty_extraction(f"AI request failed: {type(e).__name__}")
        empty["_meta"] = {
            "model": settings.anthropic_model,
            "tokens_in": None,
            "tokens_out": None,
            "claude_ms": int((_time.perf_counter() - t0) * 1000),
        }
        return empty

    raw = response.content[0].text if response.content else ""
    parsed = _parse_extraction(raw)
    usage = getattr(response, "usage", None)
    parsed["_meta"] = {
        "model": getattr(response, "model", settings.anthropic_model),
        "tokens_in":  getattr(usage, "input_tokens", None) if usage else None,
        "tokens_out": getattr(usage, "output_tokens", None) if usage else None,
        "claude_ms": int((_time.perf_counter() - t0) * 1000),
    }
    return parsed


def _empty_extraction(reply: str) -> dict:
    return {
        "ratings": [],
        "materials": [],
        "corrosion_allowances": [],
        "services": [],
        "exclude_nace": False,
        "exclude_low_temp": False,
        "rating_min": None,
        "rating_min_inclusive": False,
        "rating_max": None,
        "rating_max_inclusive": False,
        "design_temp_c": None,
        "design_pressure_barg": None,
        "intent": "unknown",
        "reply": reply,
    }


def _parse_extraction(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return _empty_extraction("Sorry — I couldn't understand that. Try again.")
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return _empty_extraction("Sorry — I couldn't understand that. Try again.")

    def _arr(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    def _num(v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _bool(v: Any) -> bool:
        return bool(v) if v is not None else False

    return {
        "ratings":              _arr(parsed.get("ratings")),
        "materials":            _arr(parsed.get("materials")),
        "corrosion_allowances": _arr(parsed.get("corrosion_allowances")),
        "services":             _arr(parsed.get("services")),
        "exclude_nace":         _bool(parsed.get("exclude_nace")),
        "exclude_low_temp":     _bool(parsed.get("exclude_low_temp")),
        "rating_min":           _num(parsed.get("rating_min")),
        "rating_min_inclusive": _bool(parsed.get("rating_min_inclusive")),
        "rating_max":           _num(parsed.get("rating_max")),
        "rating_max_inclusive": _bool(parsed.get("rating_max_inclusive")),
        "design_temp_c":        _num(parsed.get("design_temp_c")),
        "design_pressure_barg": _num(parsed.get("design_pressure_barg")),
        "intent":               (parsed.get("intent") or "unknown") if isinstance(parsed.get("intent"), str) else "unknown",
        "reply":                (parsed.get("reply") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Slot validation (per-field array → canonical array + suggestions)
# ---------------------------------------------------------------------------

def _validate_list(
    field: str, values: list[str],
) -> tuple[list[str], list[dict]]:
    """Validate each value. Service slot is free-text so anything passes;
    other slots are constrained to the catalog (with suggestions on miss)."""
    if not values:
        return [], []

    cat = _catalog()
    candidates = {
        "rating": cat["ratings"],
        "material": cat["materials"],
        "corrosion_allowance": cat["corrosion_allowances"],
        "service": cat["services"],
    }[field]

    canonical: list[str] = []
    suggestions: list[dict] = []
    seen: set[str] = set()

    for v in values:
        if field == "service":
            # Free text — accept verbatim. Try exact-catalog match first
            # so capitalisation normalises.
            m = _match_catalog(v, candidates)
            chosen = m or v
            if chosen not in seen:
                canonical.append(chosen)
                seen.add(chosen)
            continue
        m = _match_catalog(v, candidates)
        if m:
            if m not in seen:
                canonical.append(m)
                seen.add(m)
        else:
            suggestions.append({
                "field": field,
                "provided": v,
                "suggestions": _suggest(v, candidates),
            })
    return canonical, suggestions


# ---------------------------------------------------------------------------
# Filter assembly
# ---------------------------------------------------------------------------

def _apply_rating_range(
    ratings: list[str],
    rmin: Optional[float], rmin_incl: bool,
    rmax: Optional[float], rmax_incl: bool,
) -> list[str]:
    """Filter the rating list by numeric bounds. Non-numeric ratings
    (Tubing / EEMUA) pass through only when no bound is set."""
    if rmin is None and rmax is None:
        return ratings
    out: list[str] = []
    for r in ratings:
        n = _rating_to_num(r)
        if n is None:
            continue  # Tubing / EEMUA — excluded from numeric range queries
        if rmin is not None:
            if rmin_incl:
                if n < rmin:
                    continue
            else:
                if n <= rmin:
                    continue
        if rmax is not None:
            if rmax_incl:
                if n > rmax:
                    continue
            else:
                if n >= rmax:
                    continue
        out.append(r)
    return out


def _apply_material_exclusions(
    materials: list[str], exclude_nace: bool, exclude_low_temp: bool,
) -> list[str]:
    out: list[str] = []
    for m in materials:
        u = m.upper()
        if exclude_nace and "NACE" in u:
            continue
        if exclude_low_temp and (u.startswith("LTCS") or "LOW TEMP" in u):
            continue
        out.append(m)
    return out


def _resolve_filter_sets(extracted: dict) -> dict:
    """Combine validation + range + exclusion to produce the final
    filter sets used for enumeration."""
    canonical_ratings, rating_suggestions = _validate_list("rating", extracted["ratings"])
    canonical_materials, material_suggestions = _validate_list("material", extracted["materials"])
    canonical_cas, ca_suggestions = _validate_list("corrosion_allowance", extracted["corrosion_allowances"])
    canonical_services, _service_suggestions = _validate_list("service", extracted["services"])

    cat = _catalog()
    # Default to entire catalog when filter is empty for that dimension.
    rating_set = canonical_ratings or list(cat["ratings"])
    material_set = canonical_materials or list(cat["materials"])
    ca_set = canonical_cas or list(cat["corrosion_allowances"])

    # Apply numeric rating range.
    rating_set = _apply_rating_range(
        rating_set,
        extracted["rating_min"], extracted["rating_min_inclusive"],
        extracted["rating_max"], extracted["rating_max_inclusive"],
    )

    # Apply material exclusions (only useful when material isn't already
    # explicitly listed; if the user said "DSS" they meant DSS).
    if canonical_materials:
        material_set = _apply_material_exclusions(
            canonical_materials, extracted["exclude_nace"], extracted["exclude_low_temp"],
        )
    else:
        material_set = _apply_material_exclusions(
            material_set, extracted["exclude_nace"], extracted["exclude_low_temp"],
        )

    return {
        "canonical_ratings": canonical_ratings,
        "canonical_materials": canonical_materials,
        "canonical_cas": canonical_cas,
        "canonical_services": canonical_services,
        "rating_set": rating_set,
        "material_set": material_set,
        "ca_set": ca_set,
        "field_suggestions": rating_suggestions + material_suggestions + ca_suggestions,
    }


# ---------------------------------------------------------------------------
# Match enumeration
# ---------------------------------------------------------------------------

def _pt_preview(rating: str, material: str) -> str:
    pt = pt_lookup.find(rating, material)
    if not pt or pt.get("pending"):
        return "P-T data not catalogued"
    temps = pt.get("temperatures_c") or []
    pressures = pt.get("pressures_barg") or []
    if not temps or not pressures:
        return "P-T data not catalogued"
    cold = pt.get("cold_point") or {}
    hot = pt.get("hottest_point") or {}
    return (
        f"{cold.get('pressure_barg', max(pressures))} barg @ "
        f"{min(temps)}°C – {hot.get('temperature_c', max(temps))}°C"
    )


def _safe_resolve(rating: str, material: str, ca: str, service: Optional[str]) -> Optional[dict]:
    try:
        return class_resolver.resolve(
            rating=rating, material=material, ca=ca, service=service or "",
        )
    except class_resolver.ResolutionError:
        return None


def _rated_pressure_at_temp(pt: Optional[dict], design_t_c: float) -> Optional[float]:
    """Linear-interpolated flange rating (barg) at the requested design
    temperature. Returns None when no P-T table is indexed for this
    material, when the table has no entries, or when `design_t_c` is
    ABOVE the table's highest indexed point (clamping high would be a
    false ADEQUATE — better to flag it as out-of-envelope)."""
    if not pt or pt.get("pending"):
        return None
    temps = pt.get("temperatures_c") or []
    pressures = pt.get("pressures_barg") or []
    if not temps or not pressures or len(temps) != len(pressures):
        return None
    # Below the indexed minimum → clamp to the cold-end rating (max P).
    if design_t_c <= temps[0]:
        return float(pressures[0])
    # Above the indexed maximum → out of envelope. Do NOT clamp high.
    if design_t_c > temps[-1]:
        return None
    for i in range(len(temps) - 1):
        t1, t2 = temps[i], temps[i + 1]
        if t1 <= design_t_c <= t2:
            p1, p2 = pressures[i], pressures[i + 1]
            frac = (design_t_c - t1) / (t2 - t1)
            return float(p1 + (p2 - p1) * frac)
    return float(pressures[-1])


# Same tolerance the page-side adequacy banner uses (0.05 barg ≈ 0.7 psig,
# within ASME B16.5 P-T table rounding noise).
_ADEQUACY_TOLERANCE_BARG = 0.05


# Standard ASME B16.5 ratings in ascending psi order. Used by the
# over-spec check to find the smallest rating that fits a design P/T.
# Non-standard ratings (5000# / 10000# / EEMUA / Tubing) deliberately
# omitted — those have project-specific rating logic.
_STANDARD_RATING_ORDER: tuple[str, ...] = (
    "150#", "300#", "600#", "900#", "1500#", "2500#",
)


def _rating_supports_at_t(
    rating: str, material: str,
    design_p_barg: float, design_t_c: float,
) -> bool:
    """Pure 'rated@T ≥ design_p' check — no over-spec check. Used by
    `_natural_rating_for_design_point` to walk the rating ladder."""
    pt = pt_lookup.find(rating, material)
    if not pt or pt.get("pending"):
        return False
    rated = _rated_pressure_at_temp(pt, design_t_c)
    if rated is None:
        return False
    return rated + _ADEQUACY_TOLERANCE_BARG >= design_p_barg


def _natural_rating_for_design_point(
    material: str, design_p_barg: float, design_t_c: float,
) -> Optional[str]:
    """Find the smallest standard rating whose rated P at design_t_c
    is ≥ design_p_barg. Returns None if even 2500# can't sustain it
    (P is just too high for any standard class), or if the material
    isn't indexed in the standard B16.5 ladder."""
    for rating in _STANDARD_RATING_ORDER:
        if _rating_supports_at_t(rating, material, design_p_barg, design_t_c):
            return rating
    return None


def _is_adequate(
    rating: str, material: str,
    design_p_barg: Optional[float], design_t_c: Optional[float],
) -> tuple[bool, Optional[str]]:
    """Returns (adequate, reason_if_not).

    Adequacy logic — two checks:
      1. (under-spec) rated@T must be ≥ design_p_barg
      2. (over-spec) the user's rating must be the *natural* fit —
         i.e. the smallest standard rating whose rated@T ≥ design_p.
         If the user pins a much-higher rating, it's reported as
         "not appropriate" per ASME B16.5 design practice — engineers
         don't normally over-spec a flange class beyond what the
         design pressure needs.

    Special cases:
      • design_p_barg is None        → no pressure constraint, adequate.
      • design_t_c is None           → inferred via
        `pms_snapshot.effective_design_temp` (inverse interp from P
        on the curve), so the chat-agent filter agrees with what
        the snapshot will compute.
      • Non-standard rating (5000# / 10000# / EEMUA / Tubing) → skip
        the over-spec check (they use project-specific rating logic).
    """
    if design_p_barg is None:
        return True, None  # No constraint to check.

    pt = pt_lookup.find(rating, material)
    if not pt or pt.get("pending"):
        # No ASME B16.5 P-T curve for this material (e.g. GRE / CPVC /
        # composite). When the user gave design conditions, we can't
        # verify the class meets them — reject rather than mislead.
        return False, "no ASME B16.5 P-T curve indexed for this material"

    # Use the same effective T the snapshot will compute. When the user
    # supplied only P, this inverse-interpolates T from the P-T curve.
    t_c = pms_snapshot.effective_design_temp(rating, material, design_p_barg, design_t_c)
    if t_c is None:
        return False, "no P-T temperature axis to verify against"

    rated = _rated_pressure_at_temp(pt, t_c)
    if rated is None:
        # design T above the indexed envelope.
        return False, (
            f"design T {t_c}°C is above the indexed P-T envelope "
            f"(max {max(pt.get('temperatures_c') or [None])}°C)"
        )

    # Check 1 — under-spec (rated < design): class can't sustain the pressure.
    if rated + _ADEQUACY_TOLERANCE_BARG < design_p_barg:
        return False, (
            f"rated {rated:.1f} barg at {t_c:.0f}°C < design {design_p_barg:.1f} barg"
        )

    # Check 2 — over-spec: user's rating exceeds the natural fit.
    # Only applies to standard B16.5 ratings.
    if rating in _STANDARD_RATING_ORDER:
        natural = _natural_rating_for_design_point(material, design_p_barg, t_c)
        if natural and natural != rating:
            natural_rated = _rated_pressure_at_temp(
                pt_lookup.find(natural, material) or {}, t_c,
            )
            return False, (
                f"over-spec for {design_p_barg:.1f} barg @ {t_c:.0f}°C — per "
                f"ASME B16.5, class {natural} is the natural fit "
                f"(rated {natural_rated:.1f} barg at this T). "
                f"Class {rating} would be heavily over-specified."
            )

    return True, None


def _enumerate(
    rating_set: list[str],
    material_set: list[str],
    ca_set: list[str],
    service: Optional[str],
    design_p_barg: Optional[float] = None,
    design_t_c: Optional[float] = None,
    max_cards: int = 60,
) -> tuple[list[dict], int]:
    """Cartesian product of rating × material × CA, resolved to class
    codes. Combinations that don't fit §5.5 rules silently drop out.

    When `design_p_barg` is provided, every candidate is filtered through
    the P-T adequacy check — only classes that can sustain the design
    point survive. The second element of the returned tuple is the count
    of candidates rejected by the adequacy filter so the caller can
    explain "no match" to the user.

    Dedup is by (rating, class_code): the §5.5 rules sometimes resolve
    two different material/CA pairs to the same class (e.g. CS + 6 mm
    auto-promotes to NACE, so it becomes A2N — same as CS NACE + 6 mm).
    Both are the same PMS Excel, so we keep the first occurrence per
    rating + class_code pair."""
    out: list[dict] = []
    seen_tuple: set[tuple[str, str, str]] = set()
    seen_class: set[tuple[str, str]] = set()
    inadequate_count = 0
    for r in rating_set:
        for m in material_set:
            for ca in ca_set:
                key = (r, m, ca)
                if key in seen_tuple:
                    continue
                seen_tuple.add(key)
                resolved = _safe_resolve(r, m, ca, service)
                if resolved is None:
                    continue
                class_key = (r, resolved["class_code"])
                if class_key in seen_class:
                    continue
                seen_class.add(class_key)

                if design_p_barg is not None:
                    ok, _why = _is_adequate(r, m, design_p_barg, design_t_c)
                    if not ok:
                        inadequate_count += 1
                        continue

                # Apply the same customized-zone rename the snapshot
                # builder uses, so the chat match card shows the same
                # class code the InlinePMSPreview will. The "effective
                # T" call mirrors `_seed_defaults` — when the user
                # supplied only P, this inverse-interpolates T from
                # the curve, so a P that lands in the high-T region
                # (e.g. 5.5 barg @ ~425 °C on CS 150#) still triggers
                # the rename correctly.
                base = resolved["class_code"]
                effective_t = pms_snapshot.effective_design_temp(r, m, design_p_barg, design_t_c)
                display_code = pms_snapshot.customized_class_code(base, effective_t)
                out.append({
                    "piping_class":        display_code,
                    "base_class_code":     base,
                    "rating":              r,
                    "material":            m,
                    "corrosion_allowance": ca,
                    "pt_preview":          _pt_preview(r, m),
                    "score":               1.0,
                })
                if len(out) >= max_cards:
                    return out, inadequate_count
    return out, inadequate_count


# ---------------------------------------------------------------------------
# Reply text fallbacks (used when Claude returns a blank reply)
# ---------------------------------------------------------------------------

def _pretty(field: str) -> str:
    return {
        "rating":              "Pressure Rating",
        "material":            "Material",
        "corrosion_allowance": "Corrosion Allowance",
        "service":             "Service Description",
    }.get(field, field)


def _next_missing_slot(canonical_ratings, canonical_materials, canonical_cas, canonical_services):
    for f, vals in [
        ("rating", canonical_ratings),
        ("material", canonical_materials),
        ("corrosion_allowance", canonical_cas),
        ("service", canonical_services),
    ]:
        if not vals:
            return f
    return None


_TECH_REVIEW_NOTE = (
    "📝 **Note:** Please consult a technical engineer for this PMS — a "
    "non-standard solution (compact flange / hub connector / custom class) "
    "or a different rating-class may be required."
)

# Appended to the chat reply when matches exist but the design point sits
# in a "borderline" region (material-specific creep zone, very close to
# the curve's published max, etc.). The class is technically adequate but
# we want the engineer to do a human review before signing off.
_BORDERLINE_NOTE_TEMPLATE = (
    "⚠️ **We don't have a standard PMS for this P-T requirement** — {reason}. "
    "The class above is technically adequate per ASME B16.5, but the design "
    "point is at the edge of standard service.\n\n"
    "📝 **Note:** Please consult a technical engineer once and confirm this "
    "PMS is suitable for sustained operation before issuing the report."
)


def _fmt_pt_target(design_p_barg: Optional[float], design_t_c: Optional[float]) -> str:
    """Human-readable P/T target string for replies, e.g. '25 barg @ 390°C'."""
    parts: list[str] = []
    if design_p_barg is not None:
        parts.append(f"{design_p_barg:g} barg")
    if design_t_c is not None:
        parts.append(f"{design_t_c:g}°C")
    return " @ ".join(parts) if parts else "the requested design point"


def _design_caution_reason(
    matches: list[dict],
    design_t_c: Optional[float],
) -> Optional[str]:
    """Return a one-clause reason string if the matched class(es) and
    user-supplied design T fall into a "borderline" engineering zone
    that warrants a technical-engineer review. Returns None when the
    design point is comfortably inside standard service.

    Rules in priority order (first match wins):
      • Any design T above the standard envelope cap (300 °C) →
        "new-spec" zone — the PMS is renamed and the WT calc switches
        to single-point mode. Engineer should review before issuing.
      • Plain CS / LTCS at T > 370 °C → creep / graphitization
        (B31.3 creep onset for CS; B16.5 Note (1) caps prolonged use
        at 425 °C). This is a stricter sub-case of the above.
      • Any material at T within 10 °C of its catalogued max →
        at the very edge of the published P-T envelope.
    """
    if not matches or design_t_c is None:
        return None

    # Strictest first: CS / LTCS in their creep / graphitization band.
    for m in matches:
        mat = m.get("material") or ""
        if re.search(r"^\s*(?:CS|LTCS)\s*(?:NACE)?\s*$", mat, re.I) and design_t_c > 370:
            return (
                "carbon steel above 370 °C enters the creep and "
                "graphitization zone (ASME B16.5 Note 1 caps prolonged "
                "use at 425 °C)"
            )

    # New-spec zone: any material above the 300 °C envelope cap.
    if pms_snapshot.is_customized_zone(design_t_c):
        return (
            f"design T = {design_t_c:g} °C is outside the standard "
            "0–300 °C envelope — this is a customized (new-spec) PMS"
        )

    # Generic "edge of published curve" check.
    for m in matches:
        pt = pt_lookup.find(m.get("rating") or "", m.get("material") or "")
        if not pt or pt.get("pending"):
            continue
        temps = pt.get("temperatures_c") or []
        if not temps:
            continue
        curve_max = max(temps)
        if design_t_c >= curve_max - 10:
            return (
                f"design T = {design_t_c:g} °C is within 10 °C of the "
                f"catalogued curve maximum ({curve_max:g} °C) — rated "
                f"pressure derates sharply at this end"
            )

    return None


def _default_reply(
    matches: list[dict],
    field_suggestions: list[dict],
    any_filter: bool,
    inadequate_count: int = 0,
    design_p_barg: Optional[float] = None,
    design_t_c: Optional[float] = None,
    user_rating: Optional[str] = None,
    user_material: Optional[str] = None,
) -> str:
    if field_suggestions:
        f = field_suggestions[0]
        if f["suggestions"]:
            return (
                f"I don't recognise **{f['provided']}** for "
                f"{_pretty(f['field'])}. Did you mean one of these?"
            )
        return (
            f"I don't recognise **{f['provided']}** for {_pretty(f['field'])}. "
            "Pick from the catalog."
        )
    if not any_filter:
        return (
            "Tell me what PMS you need — Rating, Material, Corrosion "
            "Allowance, and Service all help me narrow down."
        )

    # Whether the user pinned both pressure AND temperature. The system
    # message is more explicit when both are given because we can name
    # the exact point we evaluated against.
    user_gave_pt = design_p_barg is not None and design_t_c is not None
    pt_target = _fmt_pt_target(design_p_barg, design_t_c)

    if not matches:
        # No matches AND the user gave design conditions → explain why.
        # Three distinct sub-cases to disambiguate:
        #   (a) user pinned a rating and that rating is OVER-SPEC for
        #       the design P at design T (per ASME B16.5 standard) →
        #       suggest the natural rating with rated@T cited.
        #   (b) user pinned a rating that is UNDER-SPEC (can't sustain
        #       the pressure) → tell them no class fits.
        #   (c) no rating pinned and adequacy filter wiped everything →
        #       generic "no PMS in catalogue" message.
        if design_p_barg is not None and inadequate_count > 0:
            # Case (a) — user pinned a rating that doesn't match the
            # standard fit per ASME B16.5. Distinguish two sub-cases
            # by comparing ladder positions:
            #   • user_rating > natural → OVER-spec  (rated >> design)
            #   • user_rating < natural → UNDER-spec (rated < design)
            # The wording differs ("far above" vs "below") and the
            # over-vs-under hint guides the engineer to the correct
            # standard class.
            if (
                user_rating
                and user_material
                and design_t_c is not None
                and user_rating in _STANDARD_RATING_ORDER
            ):
                natural = _natural_rating_for_design_point(
                    user_material, design_p_barg, design_t_c,
                )
                if natural and natural != user_rating:
                    natural_pt = pt_lookup.find(natural, user_material) or {}
                    natural_rated = _rated_pressure_at_temp(natural_pt, design_t_c)
                    user_pt = pt_lookup.find(user_rating, user_material) or {}
                    user_rated = _rated_pressure_at_temp(user_pt, design_t_c)
                    user_idx = _STANDARD_RATING_ORDER.index(user_rating)
                    natural_idx = _STANDARD_RATING_ORDER.index(natural)
                    is_over_spec = user_idx > natural_idx
                    relation = "far above" if is_over_spec else "below"
                    rated_phrase = (
                        f"rated {user_rated:.1f} barg at {design_t_c:g}°C, "
                        f"{relation} your design pressure"
                        if user_rated is not None
                        else f"{relation} your design at {design_t_c:g}°C"
                    )
                    natural_phrase = (
                        f"rated {natural_rated:.1f} barg at the same temperature"
                        if natural_rated is not None
                        else "the standard fit"
                    )
                    note_intro = (
                        "Picking a heavily over-specified rating"
                        if is_over_spec
                        else "Picking a rating that can't sustain your "
                             "design pressure"
                    )
                    return (
                        f"❌ **{design_p_barg:g} barg @ {design_t_c:g}°C is "
                        f"not an appropriate design point for class "
                        f"{user_rating}** — per ASME B16.5, class {user_rating} "
                        f"is {rated_phrase}.\n\n"
                        f"The standard fit for {pt_target} is class "
                        f"**{natural}** ({natural_phrase}). Re-issue the "
                        f"request with class {natural} and I'll generate "
                        f"that PMS.\n\n"
                        f"📝 **Note:** {note_intro} is non-standard and not "
                        f"generated automatically. If you specifically need "
                        f"class {user_rating} for non-P/T reasons, please "
                        f"consult a technical engineer."
                    )

            # Case (b) / (c) — generic "rated < design" or no-class-fits.
            if inadequate_count == 1:
                short_sentence = (
                    "The matched class falls short of that design point on "
                    "its ASME B16.5 P-T curve"
                )
            else:
                short_sentence = (
                    f"All {inadequate_count} candidate classes fall short of "
                    "that design point on their ASME B16.5 P-T curve"
                )
            return (
                f"❌ **We don't have any PMS for this P-T requirement** "
                f"({pt_target}).\n\n"
                f"{short_sentence} (evaluated at the temperature you "
                f"supplied, not the cold-end of the curve).\n\n"
                f"{_TECH_REVIEW_NOTE}"
            )
        if design_t_c is not None and design_p_barg is None:
            return (
                f"No class in the catalogue is indexed for design T = "
                f"{design_t_c:g}°C with the other filters provided. "
                "Try lowering the temperature or relaxing the filters."
            )
        return (
            "No piping class in the catalogue matches those filters. "
            "Try a different combination."
        )

    # Match(es) found — when the user pinned a P/T point, confirm we
    # checked adequacy at THAT point so they know the recommendation
    # isn't a generic one.
    if len(matches) == 1:
        m = matches[0]
        base = (
            f"Resolved class **{m['piping_class']}** — "
            f"{m['rating']} · {m['material']} · CA {m['corrosion_allowance']}."
        )
        if user_gave_pt:
            return (
                f"{base} ✓ Verified adequate at your design point "
                f"({pt_target}). Click Download Excel to grab the PMS."
            )
        return f"{base} Click Download Excel to grab the PMS."

    if user_gave_pt:
        return (
            f"Here are **{len(matches)} matching classes** — all verified "
            f"adequate at your design point ({pt_target}). "
            "Pick one or select several and download as ZIP."
        )
    return (
        f"Here are **{len(matches)} matching classes**. "
        "Pick one or select several and download as ZIP."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def chat(prompt: str, history: list[dict]) -> dict:
    extracted = _extract_filters_with_claude(prompt, history)
    filters = _resolve_filter_sets(extracted)

    # Did the user actually constrain anything (catalog OR numeric range
    # OR exclusion flag OR design conditions)? Design pressure on its own
    # is enough to trigger enumeration — the user wants us to find a
    # class that can sustain it.
    any_filter = bool(
        filters["canonical_ratings"]
        or filters["canonical_materials"]
        or filters["canonical_cas"]
        or filters["canonical_services"]
        or extracted["rating_min"] is not None
        or extracted["rating_max"] is not None
        or extracted["exclude_nace"]
        or extracted["exclude_low_temp"]
        or extracted["design_pressure_barg"] is not None
        or extracted["design_temp_c"] is not None
    )

    service_str = ", ".join(filters["canonical_services"]) if filters["canonical_services"] else ""

    inadequate_count = 0
    if any_filter:
        matched_classes, inadequate_count = _enumerate(
            filters["rating_set"],
            filters["material_set"],
            filters["ca_set"],
            service_str or None,
            design_p_barg=extracted["design_pressure_barg"],
            design_t_c=extracted["design_temp_c"],
        )
    else:
        matched_classes = []

    # Build the backward-compatible slots block.
    rating = filters["canonical_ratings"][0] if filters["canonical_ratings"] else None
    material = filters["canonical_materials"][0] if filters["canonical_materials"] else None
    ca = filters["canonical_cas"][0] if filters["canonical_cas"] else None
    service = service_str or None

    # Build human-friendly slot display strings: when the user picked
    # multiple values for a slot, show "(N) value1, value2..." so the
    # progress pill carries the count.
    def _multi_display(vals: list[str]) -> Optional[str]:
        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]
        head = ", ".join(vals[:2])
        rest = len(vals) - 2
        return f"{head}{f' (+{rest} more)' if rest > 0 else ''}"

    rating_display = _multi_display(filters["canonical_ratings"])
    material_display = _multi_display(filters["canonical_materials"])
    ca_display = _multi_display(filters["canonical_cas"])

    missing = [
        f for f, v in [
            ("rating", rating),
            ("material", material),
            ("corrosion_allowance", ca),
            ("service", service),
        ] if not v
    ]

    # "Complete" semantics: we have a result list (so the user is unblocked)
    # OR every slot has at least one value.
    complete = bool(matched_classes) or (rating and material and ca and service)

    exclusions: list[str] = []
    if extracted["exclude_nace"]:
        exclusions.append("NACE")
    if extracted["exclude_low_temp"]:
        exclusions.append("LTCS")

    slots = {
        # legacy singular fields (frontend reads these)
        "rating":              rating_display,
        "material":            material_display,
        "corrosion_allowance": ca_display,
        "service":             service,
        "missing":             missing,
        "complete":            bool(complete),
        # multi-value extensions
        "ratings":              filters["canonical_ratings"],
        "materials":            filters["canonical_materials"],
        "corrosion_allowances": filters["canonical_cas"],
        "services":             filters["canonical_services"],
        "exclusions":           exclusions,
        "rating_min":           extracted["rating_min"],
        "rating_max":           extracted["rating_max"],
    }

    cat = _catalog()
    available_values: dict[str, list[str]] = {}
    if not filters["canonical_ratings"]:
        available_values["rating"] = cat["ratings"]
    if not filters["canonical_materials"]:
        available_values["material"] = cat["materials"]
    if not filters["canonical_cas"]:
        available_values["corrosion_allowance"] = cat["corrosion_allowances"]
    if not filters["canonical_services"]:
        available_values["service"] = cat["services"]

    if len(matched_classes) == 1 and all((rating, material, ca, service)):
        m = matched_classes[0]
        suggested_action = {
            "type": "open_generator",
            "piping_class": m["piping_class"],
            "material": m["material"],
            "corrosion_allowance": m["corrosion_allowance"],
            "service": service,
            "design_pressure_barg": extracted["design_pressure_barg"],
            "design_temp_c": extracted["design_temp_c"],
        }
    elif matched_classes:
        suggested_action = {
            "type": "list_only",
            "piping_class": None,
            "material": None,
            "corrosion_allowance": None,
            "service": service,
            "design_pressure_barg": extracted["design_pressure_barg"],
            "design_temp_c": extracted["design_temp_c"],
        }
    else:
        suggested_action = {
            "type": "none",
            "piping_class": None,
            "material": None,
            "corrosion_allowance": None,
            "service": service,
            "design_pressure_barg": extracted["design_pressure_barg"],
            "design_temp_c": extracted["design_temp_c"],
        }

    # When the design-condition adequacy filter wiped out all candidates,
    # we override Claude's reply with a precise explanation — Claude has
    # no visibility into the catalog's P-T envelope and would otherwise
    # tell the user "here you go" alongside zero match cards.
    no_match_due_to_design = (
        not matched_classes
        and inadequate_count > 0
        and extracted["design_pressure_barg"] is not None
    )
    if no_match_due_to_design:
        reply = _default_reply(
            matched_classes, filters["field_suggestions"], any_filter,
            inadequate_count=inadequate_count,
            design_p_barg=extracted["design_pressure_barg"],
            design_t_c=extracted["design_temp_c"],
            user_rating=rating,
            user_material=material,
        )
    else:
        reply = extracted["reply"] or _default_reply(
            matched_classes, filters["field_suggestions"], any_filter,
            inadequate_count=inadequate_count,
            design_p_barg=extracted["design_pressure_barg"],
            design_t_c=extracted["design_temp_c"],
            user_rating=rating,
            user_material=material,
        )

    # When the chat agent found a match but the design point is at the
    # edge of standard service (e.g. CS at 400°C, in the creep zone), we
    # append a borderline warning. The class IS adequate, but a human
    # engineer should sign off before the PMS is issued. This fires
    # whether the reply came from Claude or _default_reply, so the
    # warning is always visible.
    if matched_classes:
        # Use the effective T the snapshot will see — when the user
        # supplied only P, this inverse-interpolates from the curve
        # so the caution checks the *real* design T (e.g. P=5.5 barg
        # on CS 150# → effective T ≈ 425 °C → fires the creep caution).
        m0 = matched_classes[0]
        effective_t = pms_snapshot.effective_design_temp(
            m0.get("rating"), m0.get("material"),
            extracted["design_pressure_barg"], extracted["design_temp_c"],
        )
        caution_reason = _design_caution_reason(matched_classes, effective_t)
        if caution_reason:
            reply = f"{reply}\n\n{_BORDERLINE_NOTE_TEMPLATE.format(reason=caution_reason)}"

    return {
        "reply": reply,
        "interpreted": {
            "piping_class":         None,
            "rating":               rating,
            "material":             material,
            "corrosion_allowance": ca,
            "service":              service,
            "design_temp_c":        extracted["design_temp_c"],
            "design_pressure_barg": extracted["design_pressure_barg"],
            "intent":               extracted["intent"],
        },
        "matched_classes":     matched_classes,
        "suggested_action":    suggested_action,
        "slots":               slots,
        "field_suggestions":   filters["field_suggestions"],
        "available_values":    available_values,
        "allow_bulk_download": len(matched_classes) > 1,
        # Internal-only — token usage + latency from the Claude call.
        # The route reads this for the pms_agent_queries log and strips
        # it from the response sent to the SPA.
        "_meta": extracted.get("_meta") or {},
    }


# ---------------------------------------------------------------------------
# Excel-default helpers — used by the chat-driven download endpoints
# ---------------------------------------------------------------------------

def default_design_conditions(rating: str, material: str) -> dict:
    """Sensible defaults for chat-driven downloads: cold-point pressure,
    50 °C design T, MDMT -29 °C, Seamless joint."""
    pt = pt_lookup.find(rating, material)
    if not pt or pt.get("pending"):
        return {
            "design_p_barg": 20.0,
            "design_t_c":    50.0,
            "mdmt_c":        -29.0,
            "joint_type":    "Seamless",
        }
    cold = pt.get("cold_point") or {}
    return {
        "design_p_barg": float(cold.get("pressure_barg") or 20.0),
        "design_t_c":    50.0,
        "mdmt_c":        -29.0,
        "joint_type":    "Seamless",
    }
