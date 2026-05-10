"""Industry-standard pipe + fittings specifications per material family.

Powers the Pipe & Fittings Material Assignment tab. The map below lists
the conventional ASTM / ASME / API combinations used across oil-and-gas
projects — adding a new material family is one entry, no JSON / config.

Sources for each grade:
  • Pipe              — ASME B31.3 Table A-1 (allowed material specs)
  • Fittings          — ASME B16.9 (butt-welding fittings)
  • Flange            — ASME B16.5 (forged steel flanges)
  • Forged fittings   — ASME B16.11 (socket weld + threaded)
  • Branch / weldolet — MSS SP-97 (forged branch outlet fittings)
"""
from __future__ import annotations

import re
from typing import Optional


# Order matters — first match wins. Keep most-specific rules at the top
# (e.g. SDSS before DSS, NACE-promoted CS before generic CS).
_RULES: list[tuple[re.Pattern, dict]] = [
    # --- High-pressure CS NACE: project specs API 5L X60 PSL-2 line pipe ---
    # The resolver flips the stress table to API5LX60 for 1500#/2500# CS NACE,
    # so when callers pass `rating` we promote here too. Without rating
    # we keep the generic CS row (rating-aware logic happens in lookup()).
    (re.compile(r"(?i)\bCS\s*NACE\b"), {
        "family":       "CS NACE",
        "pipe":         "ASTM A 106 Gr. B / API 5L Gr. B",
        "fittings":     "ASTM A 234 Gr. WPB",
        "flange":       "ASTM A 105N",
        "valve_body":   "ASTM A 216 Gr. WCB",
        "branch_outlet":"MSS SP 97, ASTM A 105N",
    }),
    (re.compile(r"(?i)\bLTCS\s*NACE\b|^LTCS\s+NACE"), {
        "family":       "LTCS NACE",
        "pipe":         "ASTM A 333 Gr. 6",
        "fittings":     "ASTM A 420 Gr. WPL6",
        "flange":       "ASTM A 350 Gr. LF2",
        "valve_body":   "ASTM A 352 Gr. LCB",
        "branch_outlet":"MSS SP 97, ASTM A 350 LF2",
    }),
    (re.compile(r"(?i)^LTCS\b"), {
        "family":       "LTCS",
        "pipe":         "ASTM A 333 Gr. 6",
        "fittings":     "ASTM A 420 Gr. WPL6",
        "flange":       "ASTM A 350 Gr. LF2",
        "valve_body":   "ASTM A 352 Gr. LCB",
        "branch_outlet":"MSS SP 97, ASTM A 350 LF2",
    }),
    # --- Stainless ---
    (re.compile(r"(?i)SS316L\s*NACE|TP316L\s*NACE"), {
        "family":       "SS316L NACE",
        "pipe":         "ASTM A 312 TP 316L",
        "fittings":     "ASTM A 403 WP316L-S",
        "flange":       "ASTM A 182 F316L",
        "valve_body":   "ASTM A 351 CF3M",
        "branch_outlet":"MSS SP 97, ASTM A 182 F316L",
    }),
    (re.compile(r"(?i)\bSS316L\b|TP316L"), {
        "family":       "SS316L",
        "pipe":         "ASTM A 312 TP 316L",
        "fittings":     "ASTM A 403 WP316L-S",
        "flange":       "ASTM A 182 F316L",
        "valve_body":   "ASTM A 351 CF3M",
        "branch_outlet":"MSS SP 97, ASTM A 182 F316L",
    }),
    # --- Duplex / Super-Duplex ---
    (re.compile(r"(?i)\bSDSS\b|S32750"), {
        "family":       "Super Duplex (S32750)",
        "pipe":         "ASTM A 790 UNS S32750",
        "fittings":     "ASTM A 815 UNS S32750",
        "flange":       "ASTM A 182 F53",
        "valve_body":   "ASTM A 995 6A",
        "branch_outlet":"MSS SP 97, ASTM A 182 F53",
    }),
    (re.compile(r"(?i)\bDSS\b|S31803|S32205"), {
        "family":       "Duplex (S31803)",
        "pipe":         "ASTM A 790 UNS S31803",
        "fittings":     "ASTM A 815 UNS S31803",
        "flange":       "ASTM A 182 F51",
        "valve_body":   "ASTM A 995 4A",
        "branch_outlet":"MSS SP 97, ASTM A 182 F51",
    }),
    # --- Non-ferrous ---
    (re.compile(r"(?i)\bCuNi\b|C70600|B466"), {
        "family":       "90/10 CuNi",
        "pipe":         "ASTM B 466 UNS C70600",
        "fittings":     "ASTM B 466 / B 467 UNS C70600",
        "flange":       "ASTM B 151 UNS C70600",
        "valve_body":   "ASTM B 369 UNS C96200 (Ni Al Bronze)",
        "branch_outlet":"MSS SP 97, ASTM B 564",
    }),
    (re.compile(r"(?i)\bCOPPER\b|C12200|\bB42\b"), {
        "family":       "Copper",
        "pipe":         "ASTM B 42 UNS C12200",
        "fittings":     "ASME B 16.22 (sweat / wrought)",
        "flange":       "ASME B 16.24, ASTM B 62 UNS C83600",
        "valve_body":   "ASTM B 62 UNS C83600",
        "branch_outlet":"ASME B 16.22 fitted",
    }),
    (re.compile(r"(?i)\bTITANIUM\b|\bTi\b|B861"), {
        "family":       "Titanium Gr 2",
        "pipe":         "ASTM B 861 UNS R50400 (Gr 2)",
        "fittings":     "ASTM B 363 WPT2",
        "flange":       "ASTM B 381 F2",
        "valve_body":   "ASTM B 367 C-2",
        "branch_outlet":"MSS SP 97 + B 381 F2",
    }),
    # --- Composite / plastic — not in B31.3 Table A-1, manufacturer-specific ---
    (re.compile(r"(?i)\bGRE\b|EPOXY\s*FIBRE"), {
        "family":       "Glass-Reinforced Epoxy",
        "pipe":         "Per ISO 14692 / project mfr. spec",
        "fittings":     "Per ISO 14692 / project mfr. spec",
        "flange":       "Per mfr. spec (typ. PN20 GRE flange)",
        "valve_body":   "NAB body — ASTM B 148 UNS C95800",
        "branch_outlet":"GRE saddle / lateral per mfr.",
    }),
    (re.compile(r"(?i)\bCPVC\b"), {
        "family":       "CPVC",
        "pipe":         "ASTM F 441",
        "fittings":     "ASTM F 437 / F 438 / F 439",
        "flange":       "Per mfr. (typ. SCH 80 stub end + PVC backing)",
        "valve_body":   "NAB body — ASTM B 148 UNS C95800",
        "branch_outlet":"Saddle fitting per mfr.",
    }),
    # --- Galvanised / lined CS variants share A106 Gr B base ---
    (re.compile(r"(?i)\bCS\s*GALV\b|GALV"), {
        "family":       "Galvanised CS",
        "pipe":         "ASTM A 106 Gr. B (hot-dip galvanised, ASTM A 53)",
        "fittings":     "ASTM A 234 Gr. WPB (galvanised)",
        "flange":       "ASTM A 105N (galvanised)",
        "valve_body":   "ASTM A 216 Gr. WCB (galvanised) — body, SS trim",
        "branch_outlet":"MSS SP 97, ASTM A 105N (galvanised)",
    }),
    (re.compile(r"(?i)EPOXY\s*LINED|EPOXY"), {
        "family":       "Epoxy-Lined CS",
        "pipe":         "ASTM A 106 Gr. B (epoxy lined)",
        "fittings":     "ASTM A 234 Gr. WPB (epoxy lined)",
        "flange":       "ASTM A 105N (epoxy lined RF)",
        "valve_body":   "ASTM A 216 Gr. WCB (epoxy lined)",
        "branch_outlet":"MSS SP 97, ASTM A 105N",
    }),
    # --- Tubing (instrument / chemical injection) ---
    (re.compile(r"(?i)6\s*MO|N08367|AL.*6XN"), {
        "family":       "6 MO Tubing",
        "pipe":         "ASTM A 269 / B 690 UNS N08367",
        "fittings":     "Compression / cone & thread per Swagelok 6 MO",
        "flange":       "ASTM B 462 UNS N08367",
        "valve_body":   "ASTM A 351 CN3MN",
        "branch_outlet":"Swagelok / Parker 6 MO branch",
    }),
    (re.compile(r"(?i)SS\s*316.*TUBING|316L?.*TUBING"), {
        "family":       "SS 316 / 316L Tubing",
        "pipe":         "ASTM A 269 TP 316/316L (seamless tubing)",
        "fittings":     "Compression / cone & thread per Swagelok 316",
        "flange":       "ASTM A 182 F316L",
        "valve_body":   "ASTM A 182 F316",
        "branch_outlet":"Swagelok / Parker tube branch",
    }),
    # --- Default — generic carbon steel ---
    (re.compile(r"(?i)\bCS\b|A106"), {
        "family":       "Carbon Steel",
        "pipe":         "ASTM A 106 Gr. B",
        "fittings":     "ASTM A 234 Gr. WPB",
        "flange":       "ASTM A 105N",
        "valve_body":   "ASTM A 216 Gr. WCB",
        "branch_outlet":"MSS SP 97, ASTM A 105N",
    }),
]


def lookup(material: str, rating: Optional[str] = None) -> Optional[dict]:
    """Return a {family, pipe, fittings, flange, valve_body, branch_outlet}
    spec dict for the given material. `rating` lets us upgrade CS NACE at
    1500#/2500# to API 5L X60 PSL-2 to match what the stress lookup does.

    None is returned only when material is empty / falsy."""
    if not material:
        return None

    for rx, spec in _RULES:
        if rx.search(material):
            result = dict(spec)
            # X60 promotion: high-pressure CS NACE classes ship as line pipe.
            if result["family"] == "CS NACE" and rating:
                if re.match(r"^\s*(1500|2500)\s*#?\s*$", rating):
                    result = dict(result)
                    result["family"] = "CS NACE — X60 PSL-2"
                    result["pipe"]   = "API 5L Gr. X60 PSL-2"
                    result["branch_outlet"] = "MSS SP 97, ASTM A 105N (or API 5L X60 outlet)"
            return result

    # Catch-all default (shouldn't trigger because the CS regex is broad).
    return _RULES[-1][1].copy()
