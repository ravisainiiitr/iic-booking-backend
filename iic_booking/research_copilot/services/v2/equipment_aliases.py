"""Static equipment aliases for deterministic resolution (no migration)."""

from __future__ import annotations

# alias (lower) -> search needles used against Equipment.name/code
EQUIPMENT_ALIASES: dict[str, list[str]] = {
    "fesem": ["fesem", "field emission", "fe-sem", "fe sem"],
    "sem": ["sem", "scanning electron"],
    "tem": ["tem", "transmission electron"],
    "xrd": ["xrd", "x-ray diffraction", "diffractometer"],
    "pxrd": ["pxrd", "powder x-ray", "powder xrd"],
    "afm": ["afm", "atomic force"],
    "xps": ["xps", "photoelectron"],
    "icp": ["icp", "icpms", "icp-ms"],
    "eds": ["eds", "edx", "energy dispersive"],
    "ftir": ["ftir", "infrared"],
    "raman": ["raman"],
    "bet": ["bet", "surface area"],
    "uv": ["uv-vis", "uv vis", "spectrophotometer"],
}
