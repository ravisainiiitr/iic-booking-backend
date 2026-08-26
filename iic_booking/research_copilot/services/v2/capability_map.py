"""
Phase D — capability → technique → equipment mapping (deterministic).

Maps natural-language research goals to portal equipment search needles.
Never invents instruments; only suggests search terms against the live catalog.
"""

from __future__ import annotations

# capability phrase → (technique label, search needles for Equipment name/description)
CAPABILITY_MAP: list[tuple[list[str], str, list[str]]] = [
    (["crystal structure", "phase identification", "phase id", "crystall", "diffraction", "xrd analysis", "perform xrd"], "X-ray diffraction (XRD)", ["xrd", "pxrd", "diffract"]),
    (["elemental composition", "elemental analysis", "chemical composition", "eds", "edx", "eds/edx"], "Elemental analysis (EDS/XRF)", ["eds", "xrf", "fesem", "sem"]),
    (["surface morphology", "morphology", "surface imaging", "nanoscale imaging"], "Electron microscopy (SEM/FESEM)", ["fesem", "sem", "tem"]),
    (["particle size", "particle-size"], "Particle / size analysis", ["particle", "dls", "zetasizer", "sem"]),
    (["molecular structure", "nmr", "nuclear magnetic"], "NMR spectroscopy", ["nmr"]),
    (["thermal analysis", "tga", "dta", "dsc", "thermograv"], "Thermal analysis", ["tga", "dta", "dsc", "thermal"]),
    (["thickness", "thin film", "ellips"], "Film / thickness techniques", ["ellips", "afm", "xrr"]),
    (["magnetic", "magnetization", "vsm", "epr", "esr"], "Magnetic resonance / magnetometry", ["vsm", "epr", "esr", "squid"]),
    (["xps", "photoelectron", "surface chemistry"], "X-ray photoelectron spectroscopy", ["xps"]),
    (["fluorescence", "xrf"], "X-ray fluorescence", ["xrf"]),
    (["ftir", "infrared", "ir spectroscopy"], "Infrared spectroscopy", ["ftir", "infrared"]),
    (["uv-vis", "uv vis", "absorbance"], "UV-Vis spectroscopy", ["uv", "spectrophot"]),
]


def match_capability(text: str) -> list[dict]:
    lower = (text or "").lower()
    hits: list[dict] = []
    for phrases, technique, needles in CAPABILITY_MAP:
        if any(p in lower for p in phrases):
            hits.append({"technique": technique, "needles": needles, "matched_phrases": [p for p in phrases if p in lower]})
    return hits
