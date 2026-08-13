"""AI-augmented engineering notes — provider-agnostic via app.services.ai_provider.

Used by Tab 5 (Components & Notes) to generate context-aware engineering
notes for the resolved PMS class. The deterministic engineering layer
(stress, schedules, wall thickness) stays untouched — AI only augments
human review with things an engineer might miss.

Which provider/key powers this call is resolved from the
ai_provider_configs table (admin-managed at /admin/ai-settings) via
`ai_provider.resolve_active_provider()`, falling back to the .env
ANTHROPIC_API_KEY when no admin provider is active — see that module
for the full resolution order."""
from __future__ import annotations

import json
import logging
import re

from app.config import settings
from app.services import ai_provider

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """True when a usable provider is resolvable — either an active
    admin-configured row, or the .env ANTHROPIC_API_KEY fallback."""
    return ai_provider.resolve_active_provider() is not None


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
        {notes: [...], model: "...", provider: "..."}    on success
        {error: "..."}                                   on failure
    """
    provider_obj = ai_provider.resolve_active_provider()
    if provider_obj is None:
        return {"error": "No AI provider configured. Set ANTHROPIC_API_KEY in .env, or add one at /admin/ai-settings."}

    user_prompt = _build_user_prompt(state)

    try:
        completion = provider_obj.complete(
            system_prompt=_SYSTEM_PROMPT,
            user_text=user_prompt,
            max_tokens=settings.anthropic_max_tokens,
        )
    except ai_provider.AIProviderError as e:
        logger.exception("AI provider call failed")
        return {"error": f"AI request failed: {e}"}
    except Exception as e:  # noqa: BLE001
        logger.exception("AI provider call failed")
        return {"error": f"AI request failed: {type(e).__name__}: {e}"}

    notes = _parse_notes(completion.text)

    return {
        "notes": notes,
        "model": getattr(provider_obj, "model", None),
        "provider": getattr(provider_obj, "provider", None),
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
