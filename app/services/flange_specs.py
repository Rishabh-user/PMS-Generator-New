"""Flange face / gasket / bolting / spectacle blind selection.

All four are derived from (rating, material) using well-established ASME
B16.5 / B16.20 / B16.48 + NACE conventions — no project-specific lookup
needed. Rules below mirror the most common oil & gas project practice;
override the constants if your client spec differs.

Used by Tab 5 (Components & Notes) to render the Flange, Bolts/Nuts/
Gaskets, and Spectacle Blind cards.
"""
from __future__ import annotations

import re
from typing import Optional


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _rating_num(rating: str) -> Optional[int]:
    """'150#' → 150, 'EEMUA 20 bar' → None, 'Tubing' → None."""
    if not rating:
        return None
    m = re.match(r"^\s*(\d+)\s*#?\s*$", rating.strip())
    return int(m.group(1)) if m else None


def _is_nace(material: str) -> bool:
    return bool(material) and "NACE" in material.upper()


def _is_ltcs(material: str) -> bool:
    return bool(material) and material.strip().upper().startswith("LTCS")


def _is_stainless(material: str) -> bool:
    if not material:
        return False
    u = material.upper()
    return any(k in u for k in ("SS316", "SS304", "TP316", "TP304", "DSS", "SDSS", "6 MO"))


# ──────────────────────────────────────────────────────────────────────
# Face type — ASME B16.5
# ──────────────────────────────────────────────────────────────────────
# Practice:
#   150 - 600#       → RF (Raised Face)
#   900#             → RF for non-NACE; RTJ when NACE
#   1500# / 2500#    → RTJ always (high pressure + sealing reliability)
#   EEMUA / Tubing   → RF (project default for CuNi / non-flanged tubing)
def face_type(rating: str, material: str) -> dict:
    rn = _rating_num(rating)
    nace = _is_nace(material)

    if rn is None:
        return {"code": "RF", "label": "Raised Face"}
    if rn <= 600:
        return {"code": "RF", "label": "Raised Face"}
    if rn <= 900:
        return {"code": "RTJ" if nace else "RF",
                "label": "Ring-Type Joint" if nace else "Raised Face"}
    return {"code": "RTJ", "label": "Ring-Type Joint"}


# ──────────────────────────────────────────────────────────────────────
# Bolting — A193 / A320 / A194 per material family + service
# ──────────────────────────────────────────────────────────────────────
# Standard CS bolt + nut:          A193 B7  / A194 2H
# Sour service (NACE):             A193 B7M / A194 2HM    (hardness-controlled)
# Low-temp CS (LTCS service):      A320 L7  / A194 7
# Low-temp NACE (LTCS NACE):       A320 L7M / A194 7M
# Coating: Xylan dual-layer (XYLAR 2 + XYLAN 1070) for marine / external corrosion.
_BOLT_COATING = "XYLAR 2 + XYLAN 1070 coated with minimum combined thickness of 50µm"

def bolting(material: str) -> dict:
    nace = _is_nace(material)
    ltcs = _is_ltcs(material)
    if ltcs and nace:
        stud, nut = "ASTM A 320 Gr. L7M", "ASTM A 194 Gr. 7M"
    elif ltcs:
        stud, nut = "ASTM A 320 Gr. L7",  "ASTM A 194 Gr. 7"
    elif nace:
        stud, nut = "ASTM A 193 Gr. B7M", "ASTM A 194 Gr. 2HM"
    else:
        stud, nut = "ASTM A 193 Gr. B7",  "ASTM A 194 Gr. 2H"
    return {
        "stud":    f"{stud}, {_BOLT_COATING}",
        "hex_nut": f"{nut}, {_BOLT_COATING}",
    }


# ──────────────────────────────────────────────────────────────────────
# Gasket — ASME B16.20
# ──────────────────────────────────────────────────────────────────────
# RF flange  → Spiral Wound, SS316L winding + Flexible Graphite filler
# RTJ flange → Octagonal Ring. Soft iron for general service; SS316L for
#              NACE / sour (hardness ≤ 160 BHN to satisfy MR0175).
def gasket(face: str, material: str) -> dict:
    nace = _is_nace(material)
    if face == "RTJ":
        ring_mat = "SS316L with Max. Hardness of 160 BHN" if nace else "Soft Iron"
        return {
            "type":   "RTJ Octagonal Ring",
            "spec":   f"ASME B 16.20, OCT ring of {ring_mat}",
        }
    # RF
    hardness = " with Max. Hardness of 160 BHN per NACE MR0175" if nace else ""
    return {
        "type": "Spiral Wound",
        "spec": f"ASME B 16.20, 4.5mm, SS316 Spiral Wound with Flexible Graphite (F.G.) filler{hardness}",
    }


# ──────────────────────────────────────────────────────────────────────
# Spectacle Blind / Spacer Blind — ASME B16.48
# ──────────────────────────────────────────────────────────────────────
# MOC matches the flange material family. Small-bore: paddle blind;
# Large-bore: separate blind + spacer.
def spectacle(flange_moc: Optional[str]) -> dict:
    moc = flange_moc or "—"
    return {
        "moc":           moc,
        "small_bore":    "ASME B 16.48",
        "large_bore":    "Spacer and blind as per ASME B 16.48 (Note 4)",
    }


# ──────────────────────────────────────────────────────────────────────
# Flange description (Type / Compact / Hub Connector)
# ──────────────────────────────────────────────────────────────────────
def flange_type(rating: str, material: str) -> dict:
    """Standard B16.5 Weld Neck flange description. Compact + Hub Connector
    blocks are project-conventional alternatives surfaced as informational
    rows in the Flange card."""
    rn = _rating_num(rating)
    note = " (See Note 6, 7 for sizes NPS 26\" and larger)" if rn and rn >= 600 else ""
    weld_neck = f"Weld Neck per ASME B 16.5, Butt Welding ends per ASME B 16.25{note}"

    # Compact flange (Norsok L-005) — alternative for high-pressure layouts
    compact = "WN Compact Flange (Norsok Standard L-005) — to be used for layout constraint"

    # Hub Connector (clamp) — alternative for very high pressure
    hub_seal_ring = material_ring_match(material)
    hub = (
        f"Seal Ring: {hub_seal_ring}; Hub and Blind Hub: {hub_seal_ring}; "
        f"Clamp: AISI 4140; Bolt Material as below — to be used where "
        f"ANSI or Compact Flange not suitable"
    )

    return {
        "type":     weld_neck,
        "compact":  compact,
        "hub":      hub,
    }


def material_ring_match(material: str) -> str:
    """Map material family to ring / seal ring material for Hub Connector."""
    u = (material or "").upper()
    if "SS316L" in u or "TP316L" in u:    return "ASTM A 182 F316L"
    if "SS316" in u or "TP316" in u:      return "ASTM A 182 F316"
    if "SDSS" in u or "S32750" in u:      return "ASTM A 182 F53"
    if "DSS"  in u or "S31803" in u:      return "ASTM A 182 F51"
    if u.startswith("LTCS"):              return "ASTM A 350 LF2"
    if "CUNI" in u or "C70600" in u:      return "ASTM B 564 UNS C70600"
    if "TITANIUM" in u or "B861" in u:    return "ASTM B 381 F2"
    return "ASTM A 105N"


# ──────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────
def build(rating: str, material: str, flange_moc: Optional[str]) -> dict:
    """One-shot bundle of all four sections for code_factors.flange_extras."""
    face = face_type(rating, material)
    return {
        "face":     face,
        "type":     flange_type(rating, material),
        "bolting":  bolting(material),
        "gasket":   gasket(face["code"], material),
        "spectacle": spectacle(flange_moc),
    }
