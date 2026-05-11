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
# Valves — type-based descriptions
# ──────────────────────────────────────────────────────────────────────
# We surface STANDARD descriptions (body material + seat + bore + face)
# rather than project-internal codes (`BLRPF10J` etc.) which require a
# project valve catalog to map. Engineers map description → project code
# manually; the description itself is the spec on most major-IOC PMS docs.
def _seat_choice(rating_num: int, nace: bool, austenitic: bool) -> str:
    """Soft-seated valves (PTFE) for low/medium pressure + non-NACE;
    metal-seated above 600# or for sour service / abrasive duty."""
    if nace:
        return "Metal-seated (NACE compliant)"
    if rating_num and rating_num >= 900:
        return "Metal-seated"
    return "PTFE-seated"


def _body_material_cast(material: str) -> str:
    """Map material family → conventional cast valve body grade."""
    u = (material or "").upper()
    if "SS316L" in u or "TP316L" in u:    return "ASTM A 351 CF3M"
    if "SS316"  in u or "TP316"  in u:    return "ASTM A 351 CF8M"
    if "SDSS"   in u or "S32750" in u:    return "ASTM A 995 Gr. 6A"
    if "DSS"    in u or "S31803" in u:    return "ASTM A 995 Gr. 4A"
    if u.startswith("LTCS"):              return "ASTM A 352 Gr. LCB"
    if "CUNI"   in u or "C70600" in u:    return "ASTM B 369 UNS C96200 (NAB)"
    if "COPPER" in u or "C12200" in u:    return "ASTM B 62 UNS C83600"
    if "TITANIUM" in u or "B861" in u:    return "ASTM B 367 Gr. C-2"
    if "CPVC"   in u:                     return "NAB Body — ASTM B 148 UNS C95800"
    if "GRE"    in u:                     return "NAB Body — ASTM B 148 UNS C95800"
    return "ASTM A 216 Gr. WCB"


def _class_base(class_code: Optional[str]) -> str:
    """Strip the §5.5 trailing suffix (N / L / LN) from a class code so the
    valve nomenclature uses the bare letter+digit, e.g. 'A1N' → 'A1',
    'F10N' → 'F10', 'G1LN' → 'G1'."""
    if not class_code:
        return "A1"
    return re.sub(r"L?N?$", "", class_code, flags=re.I)


def _valve_codes(rating: str, material: str, class_code: Optional[str]) -> dict:
    """Build project-style valve codes per the §5.5 nomenclature:
        [TYPE 2ch] [SUBTYPE 1ch] [SEAT 1ch] [§5.5 class base] [FACE 1ch]
    Decoded from the project's valve catalog screenshots."""
    rn   = _rating_num(rating) or 0
    nace = _is_nace(material)
    face_code = face_type(rating, material)["code"]
    face_suffix = "R" if face_code == "RF" else "J"

    base = _class_base(class_code)
    tail = f"{base}{face_suffix}"

    # Ball valve seat: Trunnion (T) for ≤600# non-NACE, Pressure-sealed
    # Metal (P) for ≥900# or any NACE class.
    ball_seat = "P" if (nace or rn >= 900) else "T"

    # Universal valves — applicable to every §5.5 class (Ball / Gate / Globe / Check)
    codes = {
        "ball":   f"BLR{ball_seat}{tail}, BLF{ball_seat}{tail}",
        "gate":   f"GAYM{tail}",
        "globe":  f"GLYM{tail}",
        "check":  f"CHPM{tail}, CHSM{tail}, CHDM{tail}",
    }

    # Butterfly — low-pressure water / utility / large-bore on-off duty.
    # Practice: 150# and 300# only, non-NACE. 600#+ uses ball/gate for
    # sealing reliability; sour service excludes butterfly entirely.
    if rn and rn <= 300 and not nace:
        codes["butterfly"] = f"BFWT{tail}, BFTP{tail}"

    # DBB (Double Block & Bleed) — positive double-isolation, mandated for
    # high-pressure sealing-critical service. Project §5.5 catalog lists
    # DBB on E (900#), F (1500#), G (2500#) classes only.
    if rn and rn >= 900:
        codes["dbb"]      = f"DBR{ball_seat}{tail}"
        codes["dbb_inst"] = f"DBR{ball_seat}{tail}T"   # "T" = threaded instrument connection
    return codes


def valves(rating: str, material: str, class_code: Optional[str] = None) -> dict:
    """Return valve specs by type. Each entry carries BOTH the project
    code (for procurement / Excel export) AND a descriptive sentence
    (for engineering review on screen)."""
    rn   = _rating_num(rating) or 0
    nace = _is_nace(material)
    face = face_type(rating, material)["code"]
    body = _body_material_cast(material)
    is_aust = "SS316" in (material or "").upper() or "TP316" in (material or "").upper()
    seat = _seat_choice(rn, nace, is_aust)
    nace_suffix = " (NACE MR0175 trim, hardness controlled)" if nace else ""

    codes = _valve_codes(rating, material, class_code)

    def _row(code_key: str, desc: str) -> dict:
        return {"code": codes.get(code_key, "—"), "desc": desc}

    result = {
        "rating":   f"{rating}, {face}",
        "body":     body,
        "ball":     _row("ball",
            f"Reduced bore (0.5\"–2\"); Reduced and Full bore (2.5\"–24\"), {seat}, {body} body, Flanged {face}{nace_suffix}"),
        "gate":     _row("gate",
            f"Y-body, Metal-seated, Screw-and-Yoke, all sizes, {body} body, Flanged {face}{nace_suffix}"),
        "globe":    _row("globe",
            f"Y-body, Metal-seated, Screw-and-Yoke, sizes 0.5\"–8\", {body} body, Flanged {face}{nace_suffix}"),
        "check":    _row("check",
            f"Piston check (0.5\"–3\"); Swing and Dual-plate check (4\"–24\"), {body} body, Flanged {face}{nace_suffix}"),
    }
    if "butterfly" in codes:
        result["butterfly"] = _row("butterfly",
            f"Wafer and Triple-Offset PTFE-seated (3\" and larger), {body} body, Flanged {face}{nace_suffix}")
    if "dbb" in codes:
        result["dbb"] = _row("dbb",
            f"Double Block and Bleed, {body} body, Flanged {face}{nace_suffix}")
        result["dbb_inst"] = _row("dbb_inst",
            f"DBB with threaded instrument connections, {body} body, Flanged {face}{nace_suffix}")
    return result


# ──────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────
def build(rating: str, material: str, flange_moc: Optional[str],
          class_code: Optional[str] = None) -> dict:
    """One-shot bundle of all five sections for code_factors.flange_extras."""
    face = face_type(rating, material)
    return {
        "face":      face,
        "type":      flange_type(rating, material),
        "bolting":   bolting(material),
        "gasket":    gasket(face["code"], material),
        "spectacle": spectacle(flange_moc),
        "valves":    valves(rating, material, class_code),
    }
