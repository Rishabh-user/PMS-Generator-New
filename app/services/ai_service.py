"""Anthropic Claude integration — AI-augmented engineering notes.

Used by Tab 5 (Components & Notes) to generate context-aware engineering
notes for the resolved PMS class. The deterministic engineering layer
(stress, schedules, wall thickness) stays untouched — AI only augments
human review with things an engineer might miss.

The system prompt is marked for prompt caching so repeated calls during
the same session are cheap. Optional graceful fallback when no API key
is configured."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Lazy-init the SDK so the app starts even when the package isn't installed.
_client: Any = None
_init_error: Optional[str] = None


def is_available() -> bool:
    """True when ANTHROPIC_API_KEY is set (the SDK may still fail at runtime
    if the key is invalid, but is_available is a quick gate for the UI)."""
    return bool((settings.anthropic_api_key or "").strip())


def _client_or_none() -> Any:
    global _client, _init_error
    if not is_available():
        return None
    if _client is not None:
        return _client
    if _init_error:
        return None
    try:
        from anthropic import Anthropic   # imported lazily — package optional
        _client = Anthropic(api_key=settings.anthropic_api_key)
        return _client
    except Exception as e:                # ImportError or auth-time error
        _init_error = str(e)
        logger.error("Anthropic init failed: %s", e)
        return None


# System prompt is stable across calls so it's a perfect fit for prompt
# caching — Anthropic charges far less for cached prefix tokens, which
# keeps the per-request cost tiny once the session is warm.
_SYSTEM_PROMPT = """\
You are a senior process-piping engineer reviewing a Piping Material
Specification (PMS) prepared by a junior engineer. Your job: generate 4-6
SHORT, ACTIONABLE engineering notes specific to the spec the junior
engineer has resolved. The notes should surface things they might miss
but should verify before sign-off.

Topics to consider (only those that apply to the given configuration):
  - NACE / sour-service compliance: HIC, SSC, hardness HRC ≤ 22, HV ≤ 250
  - Low-temperature service: Charpy V-notch impact testing at MDMT
  - PWHT requirements: triggered by wall thickness, material, service
  - NDE level: radiographic / UT requirements per joint efficiency E
  - Service-material compatibility (e.g. Hypochlorite vs GRE, sour vs CS)
  - Procurement notes: PSL-2 lead time, special grade availability
  - Construction notes: welding consumables, marking, color coding
  - Hydrotest considerations: held water quality, test medium, drying

Hard constraints — read carefully:
  - DO NOT recompute or estimate wall thickness, MAWP, allowable stress,
    or any other engineering quantity. Those are derived from B31.3 / B16.5
    deterministically by the calc engine; second-guessing them is wrong.
  - DO NOT invent material allowable stress values.
  - Keep each note under 2 sentences.
  - Cite the relevant standard (NACE MR0175, ASME B31.3 §x.y, ASME Section IX
    QW-200, etc.) where applicable.
  - Output STRICTLY as a JSON array. No markdown wrappers, no preamble,
    no trailing prose.
  - Schema: [{"title": "...", "body": "...", "category": "compliance|construction|procurement|verification"}]
"""


def generate_pms_notes(state: dict) -> dict:
    """Generate engineering notes for a resolved PMS state. Returns:
        {notes: [...], model: "...", usage: {...}}      on success
        {error: "..."}                                  on failure
    """
    if not is_available():
        return {"error": "ANTHROPIC_API_KEY not configured. Set it in .env to enable AI notes."}

    client = _client_or_none()
    if client is None:
        return {"error": f"Anthropic client unavailable: {_init_error or 'unknown error'}"}

    user_prompt = _build_user_prompt(state)

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.exception("Anthropic API call failed")
        return {"error": f"AI request failed: {type(e).__name__}: {e}"}

    raw = response.content[0].text if response.content else ""
    notes = _parse_notes(raw)

    return {
        "notes": notes,
        "model": getattr(response, "model", settings.anthropic_model),
        "usage": {
            "input_tokens":   getattr(response.usage, "input_tokens", None),
            "output_tokens":  getattr(response.usage, "output_tokens", None),
            "cache_read":     getattr(response.usage, "cache_read_input_tokens", None),
            "cache_creation": getattr(response.usage, "cache_creation_input_tokens", None),
        },
    }


def _build_user_prompt(state: dict) -> str:
    cls       = state.get("class_code")     or "—"
    rating    = state.get("rating")         or "—"
    material  = state.get("material")       or "—"
    ca        = state.get("ca")             or "—"
    service   = state.get("service")        or "—"
    design_p  = state.get("design_p_barg")
    design_t  = state.get("design_t_c")
    mdmt      = state.get("mdmt_c")
    joint     = state.get("joint_type")     or "Seamless"
    stress_table  = state.get("stress_table_label")  or "—"
    fitting_family = state.get("fitting_family") or "—"

    return (
        f"Resolved PMS class: {cls}\n"
        f"  Pressure Rating: {rating}\n"
        f"  Material: {material}\n"
        f"  Corrosion Allowance: {ca}\n"
        f"  Service: {service}\n"
        f"  Design Pressure: {design_p} barg\n"
        f"  Design Temperature: {design_t} °C\n"
        f"  MDMT: {mdmt} °C\n"
        f"  Joint Type: {joint}\n"
        f"  Stress Table: {stress_table}\n"
        f"  Fitting Family: {fitting_family}\n"
        f"\n"
        f"Generate engineering notes for this configuration. "
        f"Output STRICTLY as a JSON array of {{title, body, category}}."
    )


def _parse_notes(raw: str) -> list[dict]:
    """Extract the JSON array from the model output. The system prompt
    asks for strict JSON, but we still defend against the occasional
    leading/trailing prose by greedy-matching the outer brackets."""
    text = (raw or "").strip()
    # Direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [_clean_note(n) for n in parsed if isinstance(n, dict)]
    except json.JSONDecodeError:
        pass
    # Fallback: regex out the array
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return [_clean_note(n) for n in parsed if isinstance(n, dict)]
        except json.JSONDecodeError:
            pass
    # Last resort: surface the raw text as a single note so the engineer sees something.
    return [{"title": "AI response (unparsed)", "body": text[:1000], "category": "verification"}]


def _clean_note(n: dict) -> dict:
    return {
        "title":    str(n.get("title", "")).strip(),
        "body":     str(n.get("body", "")).strip(),
        "category": str(n.get("category", "verification")).strip().lower(),
    }
