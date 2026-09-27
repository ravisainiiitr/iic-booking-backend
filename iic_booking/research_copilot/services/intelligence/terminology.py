"""
Research terminology layer.

Maps technique abbreviations, spelling variants and research purposes to search needles. These are
only hints for finding equipment: every instrument shown to a user is looked up in the portal
database, so a technique listed here that IIC does not own simply produces no result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Technique:
    key: str
    label: str
    needles: tuple[str, ...]
    summary: str = ""
    purposes: tuple[str, ...] = field(default_factory=tuple)


TECHNIQUES: dict[str, Technique] = {
    t.key: t
    for t in (
        Technique(
            "xrd",
            "XRD (X-ray diffraction)",
            ("xrd", "x-ray diffraction", "x ray diffraction", "diffractometer"),
            "X-ray diffraction identifies crystalline phases and measures lattice parameters and crystallite size.",
            ("phase identification", "crystal structure", "crystallinity", "crystallite size", "lattice"),
        ),
        Technique(
            "pxrd",
            "PXRD (powder X-ray diffraction)",
            ("pxrd", "powder x-ray", "powder xrd"),
            "Powder X-ray diffraction identifies crystalline phases in powder samples.",
            ("powder phase",),
        ),
        Technique(
            "fesem",
            "FESEM (field emission scanning electron microscopy)",
            ("fesem", "fe-sem", "fe sem", "field emission"),
            "Field emission SEM images surface morphology at high resolution; often paired with EDS for elemental maps.",
            ("high resolution morphology", "nanostructure imaging"),
        ),
        Technique(
            "sem",
            "SEM (scanning electron microscopy)",
            ("sem", "scanning electron"),
            "Scanning electron microscopy images surface morphology and topography.",
            ("morphology", "surface morphology", "microstructure", "particle shape", "fracture surface"),
        ),
        Technique(
            "tem",
            "TEM (transmission electron microscopy)",
            ("tem", "hrtem", "transmission electron"),
            "Transmission electron microscopy images internal structure, lattice fringes and nanoparticle size.",
            ("lattice fringes", "nanoparticle size", "internal structure"),
        ),
        Technique(
            "afm",
            "AFM (atomic force microscopy)",
            ("afm", "atomic force"),
            "Atomic force microscopy maps surface topography and roughness at nanometre scale.",
            ("roughness", "surface roughness", "topography", "film thickness"),
        ),
        Technique(
            "xps",
            "XPS (X-ray photoelectron spectroscopy)",
            ("xps", "photoelectron", "esca"),
            "XPS measures surface elemental composition and chemical (oxidation) states.",
            ("oxidation state", "surface composition", "chemical state"),
        ),
        Technique(
            "eds",
            "EDS / EDX (energy dispersive X-ray spectroscopy)",
            ("eds", "edx", "edax", "energy dispersive"),
            "EDS gives elemental composition and maps, usually on an SEM or TEM.",
            ("elemental composition", "elemental analysis", "elemental mapping"),
        ),
        Technique(
            "icp",
            "ICP (ICP-MS / ICP-OES)",
            ("icp", "icpms", "icp-ms", "icp-oes", "icp oes", "inductively coupled"),
            "ICP-MS / ICP-OES measure trace and bulk element concentrations in solutions.",
            ("trace metal", "trace element", "heavy metal", "metal concentration"),
        ),
        Technique(
            "ftir",
            "FTIR (Fourier transform infrared spectroscopy)",
            ("ftir", "ft-ir", "infrared"),
            "FTIR identifies functional groups and chemical bonds.",
            ("functional group", "chemical bonds", "bonding"),
        ),
        Technique(
            "raman",
            "Raman spectroscopy",
            ("raman",),
            "Raman spectroscopy probes vibrational modes, carbon materials and molecular structure.",
            ("vibrational", "graphene layers", "d and g band"),
        ),
        Technique(
            "uv",
            "UV-Vis spectroscopy",
            ("uv-vis", "uv vis", "uv-visible", "spectrophotometer"),
            "UV-Vis spectroscopy measures absorbance, band gap and concentration.",
            ("band gap", "absorbance", "optical absorption"),
        ),
        Technique(
            "bet",
            "BET surface area analysis",
            ("bet", "surface area", "porosimetry"),
            "BET gas adsorption measures specific surface area and pore size distribution.",
            ("surface area", "pore size", "porosity"),
        ),
        Technique(
            "tga",
            "TGA (thermogravimetric analysis)",
            ("tga", "thermogravimetric"),
            "TGA measures mass change with temperature: thermal stability and composition.",
            ("thermal stability", "decomposition", "weight loss"),
        ),
        Technique(
            "dsc",
            "DSC (differential scanning calorimetry)",
            ("dsc", "differential scanning"),
            "DSC measures heat flow: melting, glass transition and crystallisation.",
            ("glass transition", "melting point", "heat flow"),
        ),
        Technique(
            "vsm",
            "VSM (vibrating sample magnetometer)",
            ("vsm", "vibrating sample", "magnetometer", "squid"),
            "VSM measures magnetic hysteresis and magnetisation.",
            ("magnetic properties", "magnetization", "hysteresis"),
        ),
        Technique(
            "epr",
            "EPR / ESR (electron paramagnetic resonance)",
            ("epr", "esr", "electron paramagnetic", "electron spin resonance"),
            "EPR detects unpaired electrons: radicals and paramagnetic centres.",
            ("radicals", "unpaired electron", "paramagnetic"),
        ),
        Technique(
            "nmr",
            "NMR spectroscopy",
            ("nmr", "nuclear magnetic"),
            "NMR elucidates molecular structure.",
            ("molecular structure", "structure elucidation"),
        ),
        Technique(
            "dls",
            "DLS / particle size analysis",
            ("dls", "dynamic light scattering", "particle size analyser", "particle size analyzer", "zeta"),
            "Dynamic light scattering measures hydrodynamic particle size and zeta potential.",
            ("particle size", "size distribution", "zeta potential", "hydrodynamic size"),
        ),
        Technique(
            "profilometer",
            "Surface profilometer",
            ("profilometer", "profilometry"),
            "A profilometer measures step height, thickness and surface roughness.",
            ("step height",),
        ),
        Technique(
            "confocal",
            "Confocal microscopy",
            ("confocal",),
            "Confocal microscopy gives optical sections and 3D fluorescence imaging.",
            ("fluorescence imaging", "cell imaging"),
        ),
        Technique(
            "printer",
            "3D printing",
            ("3d print", "3d printer", "3d printing"),
            "3D printing fabricates parts from a CAD model.",
            ("prototype", "fabricate part"),
        ),
    )
}

# Purpose phrases shared by several techniques ("elemental" -> EDS / XPS / ICP).
PURPOSE_GROUPS: dict[str, tuple[str, ...]] = {
    "elemental": ("eds", "xps", "icp"),
    "element": ("eds", "xps", "icp"),
    "composition": ("eds", "xps", "icp"),
    "morphology": ("sem", "fesem", "afm"),
    "microscopy": ("sem", "fesem", "tem", "afm"),
    "imaging": ("sem", "fesem", "tem", "afm"),
    "crystal": ("xrd", "pxrd"),
    "phase": ("xrd", "pxrd"),
    "particle size": ("dls", "tem", "sem"),
    "thermal": ("tga", "dsc"),
    "magnetic": ("vsm", "epr"),
    "roughness": ("afm", "profilometer"),
    "surface": ("afm", "xps", "bet"),
    "spectroscopy": ("ftir", "raman", "uv", "xps"),
}

_WORD_CACHE: dict[str, re.Pattern] = {}


def _word_re(term: str) -> re.Pattern:
    pat = _WORD_CACHE.get(term)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
        _WORD_CACHE[term] = pat
    return pat


def normalize(text: str) -> str:
    lower = (text or "").lower().strip()
    lower = re.sub(r"[\u2010-\u2015]", "-", lower)
    return re.sub(r"\s+", " ", lower)


def find_techniques(text: str) -> list[Technique]:
    """Techniques named explicitly in the text (word-aware so FESEM does not also yield SEM)."""
    lower = normalize(text)
    hits: list[Technique] = []
    for tech in TECHNIQUES.values():
        if any(_word_re(n).search(lower) for n in (tech.key, *tech.needles)):
            hits.append(tech)
    keys = {t.key for t in hits}
    for broad, narrow in (("sem", "fesem"), ("xrd", "pxrd")):
        if broad in keys and narrow in keys:
            stripped = lower
            for needle in sorted((narrow, *TECHNIQUES[narrow].needles), key=len, reverse=True):
                stripped = stripped.replace(needle, " ")
            if not any(_word_re(n).search(stripped) for n in (broad, *TECHNIQUES[broad].needles)):
                hits = [t for t in hits if t.key != broad]
    return hits


def techniques_for_purpose(text: str) -> list[Technique]:
    """Techniques suggested by a research purpose ("phase identification" -> XRD)."""
    lower = normalize(text)
    ordered: list[str] = []
    for tech in TECHNIQUES.values():
        if any(p in lower for p in tech.purposes):
            ordered.append(tech.key)
    for phrase, keys in PURPOSE_GROUPS.items():
        if _word_re(phrase).search(lower):
            ordered.extend(keys)
    seen: set[str] = set()
    out: list[Technique] = []
    for key in ordered:
        if key not in seen and key in TECHNIQUES:
            seen.add(key)
            out.append(TECHNIQUES[key])
    return out


_FILLER = {"the", "a", "an", "please", "pls", "equipment", "instrument", "machine", "facility", "lab", "?", "!"}


def bare_technique(text: str) -> Technique | None:
    """The message is only a technique name ("XRD", "fe-sem", "Raman?") with no verb or question."""
    lower = normalize(text).strip(" ?!.")
    if not lower or len(lower) > 40:
        return None
    words = [w for w in re.split(r"[\s,]+", lower) if w and w not in _FILLER]
    if not words or len(words) > 3:
        return None
    remainder = " ".join(words)
    for tech in TECHNIQUES.values():
        if remainder == tech.key or remainder in tech.needles:
            return tech
    return None
