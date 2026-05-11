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


def _is_ss316l(material: str) -> bool:
    """Project digit 10. SS316L flanges + bolting follow a single rule
    across all ratings: A320 L7M / A194 7M + SS316L gaskets."""
    if not material:
        return False
    u = material.upper().replace(" ", "")
    return "SS316L" in u or "TP316L" in u


# ──────────────────────────────────────────────────────────────────────
# Face type — ASME B16.5
# ──────────────────────────────────────────────────────────────────────
# Practice (project §5.5):
#   150 - 600#         → RF (Raised Face)
#   900# / 1500# / 2500# → RTJ (Ring-Type Joint) for sealing reliability
#                          — applies to all materials, NACE or not.
#   EEMUA / Tubing     → RF (project default for CuNi / non-flanged tubing)
def face_type(rating: str, material: str) -> dict:
    rn = _rating_num(rating)
    if rn is None or rn <= 600:
        return {"code": "RF", "label": "Raised Face"}
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
    ss316l = _is_ss316l(material)
    # SS316L (digit 10) — project rule: A320 L7M / A194 7M for ALL ratings,
    # NACE or not. Low-temp-capable + hardness-controlled for sour service.
    if ss316l:
        stud, nut = "ASTM A 320 Gr. L7M", "ASTM A 194 Gr. 7M"
    elif ltcs and nace:
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
# RF flange  → Spiral Wound, SS316/SS316L winding + Flexible Graphite filler
# RTJ flange → Octagonal Ring. Material follows the flange family:
#              - Stainless / NACE → SS316L (≤160 BHN for MR0175 compliance)
#              - LTCS             → Soft Iron (Low-Carbon)
#              - CS / generic     → Soft Iron
def gasket(face: str, material: str) -> dict:
    nace = _is_nace(material)
    stainless = _is_stainless(material)
    if face == "RTJ":
        if stainless or nace:
            note = " per NACE MR0175" if nace else ""
            ring_mat = f"SS316L with Max. Hardness of 160 BHN{note}"
        else:
            ring_mat = "Soft Iron"
        return {
            "type":   "RTJ Octagonal Ring",
            "spec":   f"ASME B 16.20, OCT ring of {ring_mat}",
        }
    # RF
    hardness = " with Max. Hardness of 160 BHN per NACE MR0175" if nace else ""
    return {
        "type": "Spiral Wound",
        "spec": f"ASME B 16.20, 4.5 mm, SS316/SS316L Spiral Wound with Flexible Graphite (F.G.) filler{hardness}",
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
    """The project's VDS valve nomenclature KEEPS the §5.5 suffix on the
    SPEC portion (A1N → 'A1N', G1LN → 'G1LN'), unlike some other PMS
    conventions that strip it. Verified against the project's PMS-F
    appendix valve catalogue."""
    if not class_code:
        return "A1"
    return class_code.strip().upper()


def _ball_seat_letters(rn: int, is_ltcs: bool) -> list[str]:
    """Soft seat letter(s) for Ball / DBB valves per the VDS code structure
    (M = Metal, P = PEEK, T = PTFE). Project §5.5 selection rules,
    verified against PMS-F appendix valve catalogue:

        150# / 300# (A, B):  T (PTFE) — always
        600# (D):            P (PEEK), but T (PTFE) when service is LTCS
                             (PEEK loses ductility below ~-65°C)
        900# / 1500# (E, F): P (PEEK) — always
        2500# (G):           P (PEEK) AND M (Metal) — both variants listed
    """
    if rn <= 300:
        return ["T"]
    if rn == 600:
        return ["T"] if is_ltcs else ["P"]
    if rn <= 1500:
        return ["P"]
    return ["P", "M"]


def _valve_codes(rating: str, material: str, class_code: Optional[str]) -> dict:
    """Build project-style valve codes per the VDS Code Structure:
        [TYPE 2ch] [BORE/DESIGN 1ch] [SEAT 1ch] [SPEC] [END-CONN 1+ch]

    SPEC keeps the §5.5 suffix (e.g. A1N, not A1). Verified against
    50501-SPE-80000-PP-ET-0001 Appendix VDS table — examples:

        A1     → BLRTA1R, BLFTA1R               (150#, PTFE, RF)
        A1N    → BLRTA1NR, BLFTA1NR              (150# NACE, PTFE, RF)
        D1     → BLRPD1R, BLFPD1R                (600#, PEEK, RF)
        D1L    → BLRTD1LR, BLFTD1LR              (600# LTCS reverts to PTFE)
        E1     → BLRPE1J, BLFPE1J                (900#, PEEK, RTJ)
        G1     → BLRPG1J, BLFPG1J, BLRMG1J, BLFMG1J   (2500# emits BOTH seats)
        DBB-Inst → DBRPE1JT                       (RTJ + NPT female end conn)
    """
    rn   = _rating_num(rating) or 0
    nace = _is_nace(material)
    ltcs = _is_ltcs(material)
    face_code = face_type(rating, material)["code"]
    face_suffix = "R" if face_code == "RF" else "J"

    code = _class_base(class_code)
    tail = f"{code}{face_suffix}"

    seats = _ball_seat_letters(rn, ltcs)

    # Ball — emit Reduced + Full bore for each seat letter
    ball_parts: list[str] = []
    for s in seats:
        ball_parts.append(f"BLR{s}{tail}")
        ball_parts.append(f"BLF{s}{tail}")

    codes = {
        "ball":   ", ".join(ball_parts),
        "gate":   f"GAYM{tail}",                              # GA + Y (Screw-Yoke) + M (Metal)
        "globe":  f"GLYM{tail}",
        "check":  f"CHPM{tail}, CHSM{tail}, CHDM{tail}",       # Piston + Swing + Dual-Plate, all M
    }

    # Butterfly — non-NACE only (project note 7 restricts wafer to water duty).
    #   150# / 300#: Wafer-PTFE (BFWT) + Triple-Offset-PEEK (BFTP)
    #   600#:        Triple-Offset-PEEK only
    #   900#+:       None — pressure-class uses ball/gate for sealing reliability
    if not nace:
        if rn and rn <= 300:
            codes["butterfly"] = f"BFWT{tail}, BFTP{tail}"
        elif rn == 600:
            codes["butterfly"] = f"BFTP{tail}"

    # DBB (Double Block & Bleed) — 900#+ classes only (E/F/G). Seat letters
    # follow the Ball rule, so G class emits both P and M variants.
    if rn and rn >= 900:
        dbb_parts = [f"DBR{s}{tail}" for s in seats]
        codes["dbb"] = ", ".join(dbb_parts)
        # Instrument DBB uses PEEK seat + JT end connection (RTJ + NPT female)
        codes["dbb_inst"] = f"DBR{seats[0]}{tail}T"
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
