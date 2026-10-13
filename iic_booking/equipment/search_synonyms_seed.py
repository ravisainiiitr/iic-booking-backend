"""Starting list of search synonyms, linked to the categories (by name) and equipment (by code) that exist.

Used once by a data migration; afterwards the Main Administrator edits the list in Admin Settings. Entries whose
categories and equipment are all missing are still created (unlinked) so an instrument can be linked later.
"""

from __future__ import annotations

SEED: list[dict] = [
    {
        "technique": "X-ray diffraction",
        "terms": "xrd, pxrd, x-ray diffraction, diffraction, diffractometer, powder diffraction, crystal structure, "
        "crystallinity, crystallite size, phase identification, phase analysis, lattice parameter, gixrd, "
        "grazing incidence, thin film diffraction",
        "categories": ["X-Ray Diffraction"],
        "equipment": [],
    },
    {
        "technique": "Electron backscatter diffraction",
        "terms": "ebsd, crystal orientation, grain orientation, texture, grain size, grain boundary",
        "categories": [],
        "equipment": ["EBSD"],
    },
    {
        "technique": "Scanning electron microscopy",
        "terms": "sem, fesem, fe-sem, field emission, scanning electron microscope, electron microscopy, surface morphology, "
        "morphology, microstructure, surface imaging, particle size, particle shape, topography, fractography",
        "categories": [],
        "equipment": ["APREO", "GEM300", "GEM560", "ULTRA"],
    },
    {
        "technique": "Transmission electron microscopy",
        "terms": "tem, hrtem, hr-tem, transmission electron microscope, electron microscopy, lattice fringes, saed, "
        "electron diffraction, nanoparticle size, nanostructure",
        "categories": [],
        "equipment": ["TEM"],
    },
    {
        "technique": "Energy dispersive X-ray spectroscopy (EDS)",
        "terms": "eds, edx, edax, elemental mapping, elemental composition, elemental analysis, element mapping",
        "categories": [],
        "equipment": ["APREO", "GEM300", "GEM560", "ULTRA", "TEM"],
    },
    {
        "technique": "X-ray fluorescence",
        "terms": "xrf, x-ray fluorescence, elemental composition, elemental analysis, oxide composition, major elements",
        "categories": [],
        "equipment": ["XRF"],
    },
    {
        "technique": "ICP mass spectrometry",
        "terms": "icp, icpms, icp-ms, icp ms, inductively coupled plasma, trace elements, trace metals, heavy metals, "
        "elemental composition, elemental analysis, isotope ratio, ppb",
        "categories": [],
        "equipment": ["ICPMS/MS"],
    },
    {
        "technique": "Atomic emission spectroscopy",
        "terms": "mpaes, mp-aes, aes, atomic emission, icp-oes, oes, metal concentration, elemental analysis",
        "categories": [],
        "equipment": ["MPAES"],
    },
    {
        "technique": "X-ray photoelectron spectroscopy",
        "terms": "xps, esca, photoelectron, surface chemistry, surface composition, oxidation state, chemical state, "
        "binding energy, depth profiling",
        "categories": [],
        "equipment": ["XPS", "Depth Profile"],
    },
    {
        "technique": "Ultraviolet photoelectron spectroscopy",
        "terms": "ups, work function, valence band, ionisation potential, ionization potential",
        "categories": [],
        "equipment": ["UPS"],
    },
    {
        "technique": "Thermal analysis",
        "terms": "tga, dta, dsc, tg-dta, thermogravimetric, thermogravimetry, thermal analysis, thermal stability, "
        "decomposition, weight loss, mass loss, melting point, glass transition, phase transition",
        "categories": ["Thermal Analysis"],
        "equipment": [],
    },
    {
        "technique": "UV-Vis-NIR spectroscopy",
        "terms": "uv-vis, uv vis, uvvis, uv, nir, spectrophotometer, absorbance, absorption spectrum, band gap, "
        "transmittance, reflectance, drs, diffuse reflectance",
        "categories": [],
        "equipment": ["UV-Vis"],
    },
    {
        "technique": "Photoluminescence and fluorescence lifetime",
        "terms": "pl, photoluminescence, fluorescence, fluorescence lifetime, trpl, emission spectrum, excitation spectrum, "
        "quantum yield, luminescence",
        "categories": [],
        "equipment": ["FLS"],
    },
    {
        "technique": "Electron paramagnetic resonance",
        "terms": "epr, esr, electron spin resonance, paramagnetic, free radicals, radicals, unpaired electrons",
        "categories": [],
        "equipment": ["EPR"],
    },
    {
        "technique": "Mass spectrometry",
        "terms": "mass spectrometry, mass spec, ms, maldi, maldi-tof, tof, molecular weight, molecular mass, protein mass, "
        "polymer mass, peptide",
        "categories": ["Mass Spectroscopy"],
        "equipment": [],
    },
    {
        "technique": "Nuclear magnetic resonance",
        "terms": "nmr, nuclear magnetic resonance, 1h nmr, 13c nmr, proton nmr, molecular structure",
        "categories": [],
        "equipment": ["NMR TXI"],
    },
    {
        "technique": "Magnetic measurements",
        "terms": "vsm, squid, magnetometer, magnetization, magnetisation, magnetic properties, hysteresis, m-h loop, "
        "mh loop, susceptibility, magnetic moment, zfc",
        "categories": [],
        "equipment": ["VSM", "SQUID", "PPMS"],
    },
    {
        "technique": "Physical property measurements",
        "terms": "ppms, resistivity, hall effect, heat capacity, specific heat, transport measurement, magnetoresistance, "
        "low temperature",
        "categories": ["Physical Properties Measurement"],
        "equipment": ["PPMS"],
    },
    {
        "technique": "Scanning probe / atomic force microscopy",
        "terms": "afm, atomic force microscopy, atomic force microscope, spm, stm, scanning probe, surface roughness, "
        "roughness, surface topography",
        "categories": [],
        "equipment": ["SPM"],
    },
    {
        "technique": "Raman spectroscopy",
        "terms": "raman, raman spectroscopy, micro raman, vibrational spectroscopy",
        "categories": [],
        "equipment": ["RAMAN"],
    },
    {
        "technique": "FTIR spectroscopy",
        "terms": "ftir, ft-ir, infrared, ir spectroscopy, functional groups, atr",
        "categories": [],
        "equipment": ["FTIR"],
    },
    {
        "technique": "Surface area (BET)",
        "terms": "bet, surface area, porosity, pore size, adsorption isotherm",
        "categories": [],
        "equipment": ["BET"],
    },
    {
        "technique": "Microwave sample digestion",
        "terms": "digestion, microwave digestion, acid digestion, sample digestion, sample preparation",
        "categories": [],
        "equipment": ["MSD"],
    },
    {
        "technique": "Liquid nitrogen",
        "terms": "ln2, liquid nitrogen, cryogen, nitrogen",
        "categories": [],
        "equipment": ["LN2"],
    },
    {
        "technique": "3D printing",
        "terms": "3d print, 3d printing, 3d printer, additive manufacturing, fdm, sla, resin printing, sls, bioprinting, "
        "bioprinter, prototype, prototyping, rapid prototyping",
        "categories": ["3D Printers"],
        "equipment": [],
    },
    {
        "technique": "Laser cutting and engraving",
        "terms": "laser cut, laser cutting, laser cutter, laser engraving, engraving, engrave, acrylic cutting, sheet cutting",
        "categories": [],
        "equipment": ["L1", "L2", "MLC1"],
    },
    {
        "technique": "CNC machining",
        "terms": "cnc, cnc machining, machining, milling, turning, lathe, router, vmc, machine shop, prototype, prototyping",
        "categories": [],
        "equipment": ["CNCL1", "CNCR1", "VMC1"],
    },
    {
        "technique": "3D scanning",
        "terms": "3d scan, 3d scanning, 3d scanner, lidar, reverse engineering, digitise, digitize",
        "categories": ["Scanner"],
        "equipment": [],
    },
    {
        "technique": "Filament making",
        "terms": "filament, filament extrusion, extruder, plastic recycling",
        "categories": ["Filament Maker"],
        "equipment": [],
    },
    {
        "technique": "Plastic welding",
        "terms": "welding, plastic welding, hot plate welding, joining plastics",
        "categories": ["Hot Plate Welding Machine"],
        "equipment": [],
    },
]


def seed_search_synonyms(Equipment, EquipmentCategory, SearchSynonym) -> int:
    """Create the starting list when no synonyms exist yet; returns how many entries were created."""
    if SearchSynonym.objects.exists():
        return 0
    created = 0
    for row in SEED:
        syn = SearchSynonym.objects.create(technique=row["technique"], terms=row["terms"])
        cats = [c for name in row["categories"] for c in EquipmentCategory.objects.filter(name__iexact=name)]
        eqs = [e for code in row["equipment"] for e in Equipment.objects.filter(code__iexact=code)]
        if cats:
            syn.categories.add(*cats)
        if eqs:
            syn.equipment.add(*eqs)
        created += 1
    return created
