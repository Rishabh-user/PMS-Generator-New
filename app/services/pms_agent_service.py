"""PMS-Agent chat — natural-language slot filling on top of the new
class_resolver / options catalog.

Front-end contract (kept compatible with the old pms-generator backend so
the existing PMSAgentPage UI keeps working unchanged):

    Request:
        { prompt: str, history: [{role, content}, ...] }

    Response:
        {
            reply: str,
            interpreted: {
                piping_class, rating, material, corrosion_allowance, service,
                design_temp_c, design_pressure_barg, intent
            },
            matched_classes: [
                { piping_class, rating, material, corrosion_allowance,
                  pt_preview, score }
            ],
            suggested_action: {
                type, piping_class, material, corrosion_allowance, service,
                design_pressure_barg, design_temp_c
            },
            slots: {
                rating, material, corrosion_allowance, service,
                missing: [...], complete: bool
            },
            field_suggestions: [
                { field, provided, suggestions: [...] }
            ],
            available_values: { rating?, material?, corrosion_allowance?, service? },
            allow_bulk_download: bool,
        }

How it works:
  1. Claude extracts slot values from the user's prompt + history.
  2. Each extracted value is validated against the catalog (the four
     options JSON files). Mis-spellings produce did-you-mean suggestions.
  3. When all 4 slots are filled, class_resolver.resolve() produces the
     class code → one match card.
  4. When 3 slots are filled, the missing dimension's catalog values are
     enumerated and resolved → many match cards (bulk download enabled).
  5. When <3 slots filled, the response asks for the next missing slot.
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
# Catalog loaders (single source of truth: app/data/*.json)
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
    """Drop the cached JSON so tests / re-extracts pick up edits."""
    _catalog.cache_clear()


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _match_catalog(value: str, candidates: list[str]) -> Optional[str]:
    """Exact (case-insensitive) match against the canonical catalog values."""
    if not value:
        return None
    target = _norm(value)
    for c in candidates:
        if _norm(c) == target:
            return c
    return None


def _suggest(value: str, candidates: list[str], n: int = 3) -> list[str]:
    """Up to `n` did-you-mean candidates ranked by similarity."""
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


# ---------------------------------------------------------------------------
# Claude — slot extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
You are the natural-language router for a Piping Material Specification (PMS)
chat assistant. Your only job: read the user's latest message PLUS the
conversation history and extract structured slot values.

Output STRICT JSON with this schema (no markdown, no preamble):

{
  "rating":               "<one of the catalog rating strings> | null",
  "material":             "<one of the catalog material strings, or a free-text guess if not in catalog> | null",
  "corrosion_allowance":  "<one of NIL / 1.5 mm / 3 mm / 6 mm> | null",
  "service":              "<one of the catalog service strings, or free text> | null",
  "design_temp_c":        "<number> | null",
  "design_pressure_barg": "<number> | null",
  "intent":               "generate | list | info | unknown",
  "reply":                "<one short conversational sentence asking for the next missing field, or confirming what you understood>"
}

Rules:
- Read the FULL history — slots carry forward. If the user said "150# CS 3mm"
  earlier and now says "Service: Steam", all four slots are filled.
- Output canonical strings whenever you can map to a catalog entry. Examples:
  "150 lb" → "150#", "carbon steel" → "CS", "stainless 316" → "SS316L",
  "3mm" / "3 mm" / "three mm" → "3 mm", "no CA" / "nil" → "NIL",
  "sour" / "NACE" applied to CS → "CS NACE".
- If the user names something not in the catalog (e.g. "Inconel"), still
  return their wording — the backend will detect the mismatch and suggest
  alternatives. Don't invent a canonical string.
- "intent": "generate" = wants a PMS / Excel; "list" = wants to see options;
  "info" = wants explanation; "unknown" otherwise.
- Numbers must be plain JSON numbers, not strings.
- Output JSON ONLY. No code fences, no commentary.
"""


def _extract_slots_with_claude(prompt: str, history: list[dict]) -> dict:
    """Call Claude to extract slots. Falls back to an empty dict when the
    key isn't configured or the call fails — the route then renders the
    "AI not configured" reply path."""
    if not ai_service.is_available():
        return {
            "rating":              None,
            "material":            None,
            "corrosion_allowance": None,
            "service":             None,
            "design_temp_c":       None,
            "design_pressure_barg": None,
            "intent":              "unknown",
            "reply":               (
                "AI is not configured on this server. Ask the operator to set "
                "ANTHROPIC_API_KEY in the backend .env."
            ),
        }

    client = ai_service._client_or_none()  # noqa: SLF001 — internal but stable
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
            max_tokens=600,
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
        "rating":              None,
        "material":            None,
        "corrosion_allowance": None,
        "service":             None,
        "design_temp_c":       None,
        "design_pressure_barg": None,
        "intent":              "unknown",
        "reply":               reply,
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

    return {
        "rating":              _clean_str(parsed.get("rating")),
        "material":            _clean_str(parsed.get("material")),
        "corrosion_allowance": _clean_str(parsed.get("corrosion_allowance")),
        "service":             _clean_str(parsed.get("service")),
        "design_temp_c":       _clean_num(parsed.get("design_temp_c")),
        "design_pressure_barg": _clean_num(parsed.get("design_pressure_barg")),
        "intent":              _clean_str(parsed.get("intent")) or "unknown",
        "reply":               _clean_str(parsed.get("reply")) or "",
    }


def _clean_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", ""):
        return None
    return s


def _clean_num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Slot validation
# ---------------------------------------------------------------------------

def _validate_slot(field: str, value: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    """Validate an extracted slot value against the catalog. Returns
    (canonical_value_or_none, optional_field_suggestion_dict).

    Service slot allows custom free text (services.json: allow_custom: true)
    so any non-blank string passes through verbatim.
    """
    if not value:
        return None, None

    cat = _catalog()
    if field == "rating":
        match = _match_catalog(value, cat["ratings"])
        if match:
            return match, None
        return None, {
            "field": field,
            "provided": value,
            "suggestions": _suggest(value, cat["ratings"]),
        }
    if field == "material":
        match = _match_catalog(value, cat["materials"])
        if match:
            return match, None
        return None, {
            "field": field,
            "provided": value,
            "suggestions": _suggest(value, cat["materials"]),
        }
    if field == "corrosion_allowance":
        match = _match_catalog(value, cat["corrosion_allowances"])
        if match:
            return match, None
        return None, {
            "field": field,
            "provided": value,
            "suggestions": _suggest(value, cat["corrosion_allowances"]),
        }
    if field == "service":
        match = _match_catalog(value, cat["services"])
        if match:
            return match, None
        # Service is free-text — accept as-is, no suggestion required.
        return value, None
    return None, None


# ---------------------------------------------------------------------------
# Match-card builders
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


def _build_match(rating: str, material: str, ca: str, service: Optional[str],
                 score: float = 1.0) -> Optional[dict]:
    resolved = _safe_resolve(rating, material, ca, service)
    if not resolved:
        return None
    return {
        "piping_class":        resolved["class_code"],
        "rating":              rating,
        "material":            material,
        "corrosion_allowance": ca,
        "pt_preview":          _pt_preview(rating, material),
        "score":               score,
    }


def _enumerate_matches(slots: dict, max_cards: int = 30) -> list[dict]:
    """Build match cards by expanding any unfilled slot across the catalog.

    - All 4 slots filled → exactly 1 card.
    - One slot missing → expand that dimension across the catalog.
    - Two slots missing → bounded cartesian product, capped at `max_cards`.
    - Three+ slots missing → no match cards yet (the route still asks for
      the next field).
    """
    cat = _catalog()
    ratings = [slots["rating"]] if slots.get("rating") else cat["ratings"]
    materials = [slots["material"]] if slots.get("material") else cat["materials"]
    cas = [slots["corrosion_allowance"]] if slots.get("corrosion_allowance") else cat["corrosion_allowances"]
    services = [slots["service"]] if slots.get("service") else [None]

    filled_count = sum(1 for k in ("rating", "material", "corrosion_allowance", "service")
                       if slots.get(k))
    if filled_count < 2:
        return []

    out: list[dict] = []
    for r in ratings:
        for m in materials:
            for ca in cas:
                for s in services:
                    card = _build_match(r, m, ca, s)
                    if card:
                        out.append(card)
                    if len(out) >= max_cards:
                        return out
    return out


# ---------------------------------------------------------------------------
# Reply text — used when Claude's reply is missing or generic
# ---------------------------------------------------------------------------

def _next_missing(slots: dict) -> Optional[str]:
    for f in ("rating", "material", "corrosion_allowance", "service"):
        if not slots.get(f):
            return f
    return None


def _pretty(field: str) -> str:
    return {
        "rating":              "Pressure Rating",
        "material":            "Material",
        "corrosion_allowance": "Corrosion Allowance",
        "service":             "Service Description",
    }.get(field, field)


def _default_reply(slots: dict, field_suggestions: list[dict], matches: list[dict]) -> str:
    if field_suggestions:
        f = field_suggestions[0]
        if f["suggestions"]:
            return (
                f"I don't recognise **{f['provided']}** for "
                f"{_pretty(f['field'])}. Did you mean one of these?"
            )
        return (
            f"I don't recognise **{f['provided']}** for "
            f"{_pretty(f['field'])}. Pick from the catalog."
        )
    missing = _next_missing(slots)
    if missing:
        return f"Which **{_pretty(missing)}** would you like?"
    if not matches:
        return (
            f"I have all four fields but couldn't resolve a class for "
            f"{slots['rating']} · {slots['material']} · CA {slots['corrosion_allowance']}. "
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
        f"Here are **{len(matches)} matching classes** for what you described. "
        "Pick one or select several and download as ZIP."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def chat(prompt: str, history: list[dict]) -> dict:
    extraction = _extract_slots_with_claude(prompt, history)

    # Merge slot state from prior assistant turns. The Claude prompt is
    # already told to read the full history, so most of the time the
    # extracted values themselves carry forward — but we still defensively
    # merge with any explicit slots the route side might inject later.

    raw_slots = {
        "rating":              extraction.get("rating"),
        "material":            extraction.get("material"),
        "corrosion_allowance": extraction.get("corrosion_allowance"),
        "service":             extraction.get("service"),
    }

    canonical: dict[str, Optional[str]] = {}
    field_suggestions: list[dict] = []
    for field, value in raw_slots.items():
        canonical_value, suggestion = _validate_slot(field, value)
        canonical[field] = canonical_value
        if suggestion:
            field_suggestions.append(suggestion)

    missing = [f for f in ("rating", "material", "corrosion_allowance", "service")
               if not canonical[f]]

    slots = {
        "rating":              canonical["rating"],
        "material":            canonical["material"],
        "corrosion_allowance": canonical["corrosion_allowance"],
        "service":             canonical["service"],
        "missing":             missing,
        "complete":            len(missing) == 0,
    }

    matched_classes = _enumerate_matches(canonical)

    # ── Pick a suggested action ─────────────────────────────────────
    if len(matched_classes) == 1 and slots["complete"]:
        m = matched_classes[0]
        suggested_action = {
            "type":                 "open_generator",
            "piping_class":         m["piping_class"],
            "material":             m["material"],
            "corrosion_allowance": m["corrosion_allowance"],
            "service":              canonical["service"],
            "design_pressure_barg": extraction.get("design_pressure_barg"),
            "design_temp_c":        extraction.get("design_temp_c"),
        }
    elif matched_classes:
        suggested_action = {
            "type":                 "list_only",
            "piping_class":         None,
            "material":             None,
            "corrosion_allowance": None,
            "service":              canonical["service"],
            "design_pressure_barg": extraction.get("design_pressure_barg"),
            "design_temp_c":        extraction.get("design_temp_c"),
        }
    else:
        suggested_action = {
            "type":                 "none",
            "piping_class":         None,
            "material":             None,
            "corrosion_allowance": None,
            "service":              canonical["service"],
            "design_pressure_barg": extraction.get("design_pressure_barg"),
            "design_temp_c":        extraction.get("design_temp_c"),
        }

    available_values: dict[str, list[str]] = {}
    cat = _catalog()
    if not canonical["rating"]:
        available_values["rating"] = cat["ratings"]
    if not canonical["material"]:
        available_values["material"] = cat["materials"]
    if not canonical["corrosion_allowance"]:
        available_values["corrosion_allowance"] = cat["corrosion_allowances"]
    if not canonical["service"]:
        available_values["service"] = cat["services"]

    reply = extraction.get("reply") or _default_reply(slots, field_suggestions, matched_classes)

    return {
        "reply":               reply,
        "interpreted": {
            "piping_class":         None,
            "rating":               canonical["rating"],
            "material":             canonical["material"],
            "corrosion_allowance": canonical["corrosion_allowance"],
            "service":              canonical["service"],
            "design_temp_c":        extraction.get("design_temp_c"),
            "design_pressure_barg": extraction.get("design_pressure_barg"),
            "intent":               extraction.get("intent", "unknown"),
        },
        "matched_classes":     matched_classes,
        "suggested_action":    suggested_action,
        "slots":               slots,
        "field_suggestions":   field_suggestions,
        "available_values":    available_values,
        "allow_bulk_download": len(matched_classes) > 1,
    }


# ---------------------------------------------------------------------------
# Excel-default helpers — used by the chat-driven download endpoints
# ---------------------------------------------------------------------------

def default_design_conditions(rating: str, material: str) -> dict:
    """Pick sensible default design P/T for chat-driven downloads.

    Strategy: take the cold-point pressure (most demanding) and 50 °C as
    a generic default temperature. MDMT defaults to -29 °C and joint type
    to Seamless. Engineers can re-run the configurator with their own
    values if they need something specific."""
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
