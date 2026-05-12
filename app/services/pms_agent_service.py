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
from app.services import ai_service, class_resolver, pt_lookup


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
9. Numbers are JSON numbers, not strings. Booleans default to false.
10. Output JSON ONLY. No code fences, no commentary.
"""


def _extract_filters_with_claude(prompt: str, history: list[dict]) -> dict:
    """Call Claude to extract structured filters. Falls back to a stub
    when the key isn't configured or the call fails."""
    if not ai_service.is_available():
        return _empty_extraction(
            "AI is not configured on this server. Ask the operator to set "
            "ANTHROPIC_API_KEY in the backend .env."
        )

    client = ai_service._client_or_none()  # noqa: SLF001
    if client is None:
        return _empty_extraction("AI client unavailable; check ANTHROPIC_API_KEY.")

    messages: list[dict] = []
    for turn in (history or []):
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": prompt})

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
        return _empty_extraction(f"AI request failed: {type(e).__name__}")

    raw = response.content[0].text if response.content else ""
    return _parse_extraction(raw)


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


def _enumerate(
    rating_set: list[str],
    material_set: list[str],
    ca_set: list[str],
    service: Optional[str],
    max_cards: int = 60,
) -> list[dict]:
    """Cartesian product of rating × material × CA, resolved to class
    codes. Combinations that don't fit §5.5 rules silently drop out.

    Dedup is by (rating, class_code): the §5.5 rules sometimes resolve
    two different material/CA pairs to the same class (e.g. CS + 6 mm
    auto-promotes to NACE, so it becomes A2N — same as CS NACE + 6 mm).
    Both are the same PMS Excel, so we keep the first occurrence per
    rating + class_code pair."""
    out: list[dict] = []
    seen_tuple: set[tuple[str, str, str]] = set()
    seen_class: set[tuple[str, str]] = set()
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
                out.append({
                    "piping_class":        resolved["class_code"],
                    "rating":              r,
                    "material":            m,
                    "corrosion_allowance": ca,
                    "pt_preview":          _pt_preview(r, m),
                    "score":               1.0,
                })
                if len(out) >= max_cards:
                    return out
    return out


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


def _default_reply(
    matches: list[dict], field_suggestions: list[dict], any_filter: bool,
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
    if not matches:
        return (
            "No piping class in the catalogue matches those filters. "
            "Try a different combination."
        )
    if len(matches) == 1:
        m = matches[0]
        return (
            f"Resolved class **{m['piping_class']}** — "
            f"{m['rating']} · {m['material']} · CA {m['corrosion_allowance']}. "
            "Click Download Excel to grab the PMS."
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
    # OR exclusion flag)?
    any_filter = bool(
        filters["canonical_ratings"]
        or filters["canonical_materials"]
        or filters["canonical_cas"]
        or filters["canonical_services"]
        or extracted["rating_min"] is not None
        or extracted["rating_max"] is not None
        or extracted["exclude_nace"]
        or extracted["exclude_low_temp"]
    )

    service_str = ", ".join(filters["canonical_services"]) if filters["canonical_services"] else ""

    matched_classes = (
        _enumerate(
            filters["rating_set"],
            filters["material_set"],
            filters["ca_set"],
            service_str or None,
        )
        if any_filter
        else []
    )

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

    reply = extracted["reply"] or _default_reply(matched_classes, filters["field_suggestions"], any_filter)

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
