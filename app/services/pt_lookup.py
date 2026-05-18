"""Pressure-Temperature table lookup.

Given a (rating, material) pair, find the matching ASME B16.5 group in
`app/data/pt_tables.json` automatically — the JSON's per-group `materials`
arrays drive material→group mapping, so adding a new material is a JSON
edit, not a code change. No hardcoded material→group dict anywhere.

Returned shape (or None when no data):
    {
        "group":           "1.1",
        "temperatures_c":  [...],
        "pressures_barg":  [...],
        "temp_labels":     [...],
        "cold_point":      {"pressure_barg": 19.6, "temperature_c": 38},
        "hottest_point":   {"pressure_barg": 10.2, "temperature_c": 300},
    }
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from app.config import settings


# Cap the on-screen "Pressure-Temperature Rating" table at this T.
# Our ASME B16.5 Group 1.1 / 2.3 / 2.8 entries extend to 538 / 450 /
# 400 °C, but the SPA / page only shows up to here so the table reads
# at typical operating range. The FULL curve stays in
# `pressure_temperature.temperatures_c` etc. — only the additional
# `pressure_temperature.display_columns` view is filtered. Interpolation,
# adequacy, WT calc, and the SPA's two-way curve sync all still walk
# the full curve.
PT_TABLE_DISPLAY_CAP_C = 300.0


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((settings.data_dir / "pt_tables.json").read_text(encoding="utf-8"))


def reload() -> None:
    _data.cache_clear()


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def _rating_key(rating: str) -> str:
    """'150#' → '150', '1500#' → '1500'. Whitespace + '#' stripped.

    Returns the original (uppercased) string for non-numeric ratings like
    'Tubing' / 'EEMUA 20 bar' so a future JSON could index those too."""
    cleaned = _norm(rating).rstrip("#").strip()
    if cleaned.isdigit():
        return cleaned
    return cleaned


def _clean_material(material: str) -> str:
    """Strip parenthetical qualifiers ('(Valve: SS)' / '(Tubing)') so user
    input matches JSON entries that omit them. Keeps NACE / LTCS markers
    because the JSON's `materials` arrays list those variants explicitly."""
    raw = _norm(material)
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw


# Materials whose P-T rating is governed by a standard OTHER than the
# user's selected flange rating. CuNi pipes (per EEMUA 234) are always
# rated at 20 bar regardless of whether they're installed in a "150#"
# system context — so when a caller asks for ("150#", "CuNi") we
# transparently redirect to ("EEMUA 20 bar", "CuNi") and surface the
# real engineering curve. Add new entries here when more materials
# follow a separate spec (e.g. dedicated tubing ratings).
_MATERIAL_RATING_REDIRECTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b(?:CuNi|90/10\s*CuNi|C70600)\b"), "EEMUA 20 bar"),
]


def _redirect_rating(rating: str, material: str) -> str:
    """Return the effective rating for the P-T lookup. For most
    materials this is the user-supplied rating. For materials whose
    P-T curve lives under a different rating bucket (see
    `_MATERIAL_RATING_REDIRECTS`), the bucket name takes over."""
    for pattern, target in _MATERIAL_RATING_REDIRECTS:
        if pattern.search(material or ""):
            return target
    return rating


# Service-specific P-T alternates. Some materials have a primary P-T
# curve plus one or more service-bound alternates (e.g. GRE pipe has a
# Hypochlorite/BONSTRAND alternate with a lower pressure and lower max
# temp). The JSON shape is:
#
#   "GRE": {
#       "materials":      [...],
#       "temperatures_c": [...],          # ← primary
#       "pressures_barg": [...],
#       "_alternates": {
#           "Hypochlorite": {              # ← service trigger (regex)
#               "temperatures_c": [...],
#               "pressures_barg": [...],
#               ...
#           }
#       }
#   }
#
# Each alternate key is matched against the user's service string via
# this regex map; on a match, the alternate's fields override the
# primary block's. Order matters — first match wins.
_SERVICE_ALTERNATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)Hypochlorite|BONSTRAND"), "Hypochlorite"),
]


def _resolve_service_alternate(group: dict, service: Optional[str]) -> dict:
    """If `service` triggers one of the alternate keys defined in
    `group._alternates`, merge that alternate over the group's primary
    fields and return the merged dict. Otherwise return the group
    untouched. Pure function — never mutates the input."""
    if not service:
        return group
    alternates = group.get("_alternates") or {}
    if not alternates:
        return group
    for pattern, alt_key in _SERVICE_ALTERNATE_PATTERNS:
        if alt_key in alternates and pattern.search(service):
            merged = dict(group)
            merged.update(alternates[alt_key])
            return merged
    return group


def find(rating: str, material: str, service: Optional[str] = None) -> Optional[dict]:
    """Locate the (rating, group) entry whose `materials` array contains
    `material`. Returns None when no match — caller decides what to do
    (show "no data", call AI fallback, etc.).

    `service` is optional. When supplied, it lets the lookup swap in a
    service-specific alternate (e.g. GRE + Hypochlorite uses the
    BONSTRAND 50000C P-T curve, which is lower-pressure and
    lower-max-temp than standard GRE)."""
    if not rating or not material:
        return None

    # Some materials have their P-T governed by a separate-standard
    # rating (e.g. CuNi → EEMUA 20 bar). Apply that redirect BEFORE
    # the rating-key lookup so the caller doesn't have to know which
    # standard a given material belongs to.
    effective_rating = _redirect_rating(rating, material)
    rating_key = _rating_key(effective_rating)
    # Case-insensitive rating-key match so 'EEMUA 20 bar' / 'EEMUA 20 BAR' /
    # 'eemua 20 bar' all hit the same JSON entry.
    rating_block = None
    for k, v in _data().get("ratings", {}).items():
        if _norm(k).rstrip("#").strip() == rating_key:
            rating_block = v
            break
    if not rating_block:
        return None
    if rating_block.get("pending"):
        # Indexed, but no authoritative data yet — surface to caller so the
        # UI can show "pending engineering review" rather than a blank.
        return {"pending": rating_block["pending"], "rating": rating_key}

    groups = rating_block.get("groups", {})
    target = _clean_material(material)

    for group_id, group in groups.items():
        # Apply the same paren-stripping clean to the JSON material strings
        # so a JSON entry of 'SS 316 / 316L (Tubing)' matches a user input
        # of 'SS 316 / 316L (Tubing)' (paren stripped on both sides).
        materials_norm = {_clean_material(m) for m in group.get("materials", [])}
        if target in materials_norm:
            # Service-aware override: GRE + Hypochlorite, etc. The
            # alternate's temperatures / pressures / hydrotest replace
            # the primary's; everything else (materials list, _alternates
            # key itself) is inherited.
            group = _resolve_service_alternate(group, service)
            temps    = list(group.get("temperatures_c", []))
            pressures = list(group.get("pressures_barg", []))
            labels   = list(group.get("temp_labels", []))

            cold_idx = pressures.index(max(pressures)) if pressures else None
            hot_idx  = temps.index(max(temps)) if temps else None

            # Pre-filtered columns for the SPA / page's P-T Rating table.
            # Backend decides what's visible; the SPA just renders this
            # subset and doesn't carry any cap value of its own.
            visible_idxs = [i for i, t in enumerate(temps) if t <= PT_TABLE_DISPLAY_CAP_C]
            display_temps    = [temps[i]     for i in visible_idxs]
            display_pressures = [pressures[i] for i in visible_idxs]
            display_labels   = [labels[i]    for i in visible_idxs] if labels else []

            return {
                "group":           group_id,
                "temperatures_c":  temps,
                "pressures_barg":  pressures,
                "temp_labels":     labels,
                # Pass through project-tabulated hydrotest if present
                # (e.g. tubing classes carry their own value).
                "hydrotest_barg":  group.get("hydrotest_barg"),
                "cold_point": (
                    {"pressure_barg": pressures[cold_idx], "temperature_c": temps[cold_idx]}
                    if cold_idx is not None else None
                ),
                "hottest_point": (
                    {"pressure_barg": pressures[hot_idx], "temperature_c": temps[hot_idx]}
                    if hot_idx is not None else None
                ),
                # On-screen P-T Rating table reads these (cap = 300 °C).
                # The full curve in `temperatures_c` / `pressures_barg`
                # above is still used for adequacy + interpolation +
                # WT calc, regardless of what we display here.
                "display_columns": {
                    "temperatures_c": display_temps,
                    "pressures_barg": display_pressures,
                    "temp_labels":    display_labels,
                    "cap_c":          PT_TABLE_DISPLAY_CAP_C,
                },
            }

    return None
