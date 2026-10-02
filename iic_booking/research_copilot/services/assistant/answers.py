"""
Deterministic answers for the questions the Booking Assistant used to log as unanswered.

Technique terms (FWHM, XRD, Bragg's law, ...), analysis software / data files / remote analysis, lab
hours, sample preparation and submission, cost estimates for N samples, who pays (PI pricing), short
acknowledgements and one-word questions. Portal facts come from live data (equipment instructions,
charge engine, software catalog, the user's own bookings); general science is a short textbook summary.
No LLM call is made.
"""

from __future__ import annotations

import re
from typing import Any

from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import matching

_EXTRA_STOP = {
    "software", "softwares", "recommend", "files", "file", "data", "analyze", "analyse", "analysis", "analyzed",
    "analysed", "analyzing", "remote", "remotely", "prepare", "preparing", "preparation", "prepared", "pi", "owner",
    "wallet", "coming", "come", "measurement", "measurements", "thin", "film", "films", "thin-film", "workstation",
    "workstations", "pc", "computer", "session", "imagej", "fiji", "dm4", "dm3", "send", "download", "happened",
    "happen", "submit", "submission", "difference", "briefly", "explain", "law", "bragg", "bragg's", "fwhm", "access",
    "policy", "quantity", "it", "cost", "will", "charged", "billed", "priced", "as", "am", "bring", "carry", "kind",
}


def _phrase(text: str) -> str:
    words = [w for w in matching.equipment_phrase(text).split() if w not in _EXTRA_STOP]
    return " ".join(words)


def resolve_equipment(user, conversation, text: str, *, use_context: bool = True):
    """(equipment, candidates): a unique visible match, else up to 3 candidates, else the conversation's equipment."""
    phrase = _phrase(text)
    if phrase:
        m = matching.match_equipment(user, phrase)
        if m.status == "unique" and m.equipment is not None:
            return m.equipment, []
        if m.status in ("options", "unavailable") and m.candidates:
            return None, [c.eq for c in m.candidates[:3]]
    if use_context and conversation is not None:
        from iic_booking.research_copilot.services.assistant import engine
        from iic_booking.research_copilot.services.assistant import state as ba_state

        try:
            return engine._context_equipment(user, conversation, ba_state.load(conversation)), []
        except Exception:  # noqa: BLE001
            return None, []
    return None, []


def _remember(conversation, eq) -> None:
    from iic_booking.research_copilot.services.assistant import state as ba_state

    if conversation is not None and eq is not None:
        ba_state.remember_equipment(conversation, eq, None, "info")


def _eq_chips(eq, *, skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    from django.utils import timezone

    from iic_booking.research_copilot.services.assistant.availability import _next7

    out = []
    if "book" not in skip and (eq.status or "").strip() == "ACTIVE":
        out.append(C.flow_action(f"Book {eq.name}"[:40], "equipment", {"equipment_id": int(eq.pk)}, primary=True))
    if "slots" not in skip and (eq.status or "").strip() == "ACTIVE":
        out.append(C.assistant_action("Free slots this week", "ba_availability",
                                      {"equipment_id": int(eq.pk), "when": _next7(timezone.localdate())},
                                      utterance=f"Free {eq.name} slots this week"))
    if "charges" not in skip:
        out.append(C.assistant_action("Check charges", "ba_info", {"equipment_id": int(eq.pk), "topic": "charges"},
                                      utterance=f"Charges for {eq.name}"))
    return out


def _upcoming(user, eq=None):
    from iic_booking.research_copilot.services.assistant import bookings as B

    qs, _ = B._filtered(user, "upcoming", None, None)
    if eq is not None:
        qs = qs.filter(equipment_id=eq.pk)
    return qs.first()


# =============================================================================== small talk / vague


def smalltalk(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant.next_steps import starter_actions

    return C.reply("You're welcome. Is there anything else I can help with?", actions=starter_actions(user)[:4],
                   intent="smalltalk", kind="ANSWER")


def vague(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant.next_steps import starter_actions

    return C.reply(
        "Could you tell me a little more? For example: \"Can I book FESEM tomorrow?\", \"How much do 5 XRD samples "
        "cost?\", \"What should I prepare before my booking?\" or \"Where are my results?\". Or pick one of these:",
        actions=starter_actions(user)[:4],
        intent="vague",
        kind="CLARIFICATION",
    )


_FIND_GOALS = (
    ("Elemental composition", "Which equipment can measure elemental composition?"),
    ("Crystal structure / phase", "Which equipment can do x-ray diffraction?"),
    ("Surface morphology / imaging", "Which equipment can do electron microscopy?"),
    ("Chemical bonds / functional groups", "Which equipment can do FTIR?"),
    ("Surface area / porosity", "Which equipment can measure surface area?"),
    ("Thermal stability", "Which equipment can do thermogravimetric analysis?"),
)


def find_equipment(user, conversation, params, text):
    return C.reply(
        "What do you want to learn about your sample? Pick the closest goal and I'll list the instruments that "
        "can do it, or type it in your own words (for example \"which equipment can measure particle size?\").",
        actions=[C.prompt_action(label, prompt) for label, prompt in _FIND_GOALS] + [C.link("Browse equipment", "/equipments")],
        intent="find_equipment",
        kind="CLARIFICATION",
        title_hint="Find equipment",
    )


# =============================================================================== glossary

GLOSSARY: dict[str, dict[str, str]] = {
    "fwhm": {
        "title": "FWHM (full width at half maximum)",
        "text": "the width of a peak measured halfway up its height. In XRD it tells you how broad a diffraction peak "
                "is: broader peaks (larger FWHM) mean smaller crystallites and/or more lattice strain, and the "
                "Scherrer equation uses it to estimate crystallite size. In spectroscopy (Raman, PL, XPS) it describes "
                "how sharp a band is.",
        "short": "FWHM measures how broad a peak is",
        "query": "XRD",
    },
    "bragg": {
        "title": "Bragg's law",
        "text": "**nλ = 2d sin θ**. X-rays of wavelength λ reflect constructively from crystal planes spaced d apart "
                "only at angles θ that satisfy this equation, so each XRD peak position (2θ) gives a d-spacing. "
                "Those d-spacings identify the crystal phase and its lattice parameters.",
        "short": "Bragg's law links peak angle to the spacing between crystal planes",
        "query": "XRD",
    },
    "scherrer": {
        "title": "Scherrer equation (crystallite size)",
        "text": "**D = Kλ / (β cos θ)**, where β is the peak FWHM in radians (after subtracting instrument broadening), "
                "θ the Bragg angle, λ the X-ray wavelength and K ≈ 0.9. It gives an average crystallite size, "
                "reliable roughly below 100 nm.",
        "short": "the Scherrer equation estimates crystallite size from peak width",
        "query": "XRD",
    },
    "gixrd": {
        "title": "Thin-film XRD (grazing-incidence XRD, GIXRD)",
        "text": "XRD can measure thin films. In grazing-incidence mode the X-rays hit the film at a very small angle, so "
                "they travel further through the film and less signal comes from the substrate. Whether a particular "
                "instrument has the thin-film attachment is listed on its equipment page, or ask its operator.",
        "short": "GIXRD measures thin films at a grazing angle",
        "query": "XRD",
    },
    "xrd": {
        "title": "XRD (X-ray diffraction)",
        "text": "shines X-rays on a sample and records diffracted intensity against angle (2θ). The peak pattern is a "
                "fingerprint of the crystal structure: it identifies phases, measures lattice parameters, crystallite "
                "size, strain and texture. Works on powders, thin films and flat solids.",
        "short": "XRD tells you the crystal structure and phases",
        "query": "XRD",
    },
    "pxrd": {
        "title": "PXRD (powder X-ray diffraction)",
        "text": "XRD on a finely ground powder, so the crystallites point in all directions. The pattern is matched "
                "against reference databases (ICDD PDF, COD) to identify phases and check purity and crystallinity; "
                "Rietveld refinement can extract lattice parameters and phase fractions.",
        "short": "PXRD identifies the phases in a powder",
        "query": "XRD",
    },
    "sem": {
        "title": "SEM (scanning electron microscope)",
        "text": "scans a focused electron beam over the sample surface to image its shape and texture (morphology) from "
                "millimetres down to a few nanometres. Non-conductive samples usually need a thin gold or carbon "
                "coating. An attached EDS detector adds elemental composition.",
        "short": "SEM shows surface morphology",
        "query": "SEM",
    },
    "fesem": {
        "title": "FESEM (field-emission SEM)",
        "text": "an SEM with a field-emission electron gun, giving sharper images at higher magnification and at lower "
                "voltages (useful for delicate or non-conductive samples). Often fitted with EDS for elemental maps.",
        "short": "FESEM gives high-resolution surface images",
        "query": "FESEM",
    },
    "tem": {
        "title": "TEM (transmission electron microscope)",
        "text": "sends electrons through a very thin sample (usually under 100 nm) to image internal structure, particle "
                "size and shape, lattice fringes (HRTEM) and diffraction patterns (SAED). Samples are dispersed or cut "
                "thin and placed on a TEM grid.",
        "short": "TEM images internal structure down to atomic lattices",
        "query": "TEM",
    },
    "eds": {
        "title": "EDS / EDX (energy-dispersive X-ray spectroscopy)",
        "text": "measures the X-rays a sample emits under the electron beam to tell which elements are present, roughly "
                "how much, and where (elemental maps). It is an attachment on SEM/FESEM/TEM.",
        "short": "EDS gives elemental composition",
        "query": "EDS",
    },
    "xps": {
        "title": "XPS (X-ray photoelectron spectroscopy)",
        "text": "analyses the top ~5–10 nm of a surface: which elements are present and their chemical/oxidation state. "
                "Samples must be dry and vacuum-compatible.",
        "short": "XPS gives surface chemistry and oxidation states",
        "query": "XPS",
    },
    "ftir": {
        "title": "FTIR (Fourier-transform infrared spectroscopy)",
        "text": "measures which infrared frequencies a sample absorbs, identifying chemical bonds and functional groups "
                "(for example O–H, C=O, N–H).",
        "short": "FTIR identifies functional groups",
        "query": "FTIR",
    },
    "raman": {
        "title": "Raman spectroscopy",
        "text": "shines a laser on the sample and measures the small energy shifts of scattered light, which reveal "
                "molecular and lattice vibrations: used for carbon materials (D and G bands), phases, stress and "
                "chemical identification.",
        "short": "Raman reveals vibrational fingerprints",
        "query": "Raman",
    },
    "bet": {
        "title": "BET surface area analysis",
        "text": "measures how much nitrogen gas a degassed powder adsorbs to calculate its specific surface area (m²/g) "
                "and pore size distribution.",
        "short": "BET measures surface area and porosity",
        "query": "BET",
    },
    "tga": {
        "title": "TGA (thermogravimetric analysis)",
        "text": "records sample mass while heating, showing moisture loss, decomposition steps, thermal stability and "
                "residue/filler content.",
        "short": "TGA tracks mass loss with temperature",
        "query": "TGA",
    },
    "dsc": {
        "title": "DSC (differential scanning calorimetry)",
        "text": "measures heat flow into or out of a sample while heating or cooling, giving melting point, "
                "crystallisation, glass transition and reaction enthalpies.",
        "short": "DSC measures thermal transitions",
        "query": "DSC",
    },
    "afm": {
        "title": "AFM (atomic force microscope)",
        "text": "scans a sharp tip across a surface to map its 3-D topography and roughness at nanometre scale; "
                "some modes also map mechanical, electrical or magnetic properties.",
        "short": "AFM maps nanoscale topography",
        "query": "AFM",
    },
    "nmr": {
        "title": "NMR (nuclear magnetic resonance)",
        "text": "places the sample in a strong magnetic field and probes nuclei such as ¹H and ¹³C to work out molecular "
                "structure, purity and composition, usually in solution.",
        "short": "NMR determines molecular structure",
        "query": "NMR",
    },
    "icp": {
        "title": "ICP-MS / ICP-OES",
        "text": "measure element concentrations (down to ppb with ICP-MS) in a liquid; solid samples are first digested "
                "in acid.",
        "short": "ICP measures trace element concentrations",
        "query": "ICP-MS",
    },
    "uvvis": {
        "title": "UV-Vis spectroscopy",
        "text": "measures how much ultraviolet and visible light a sample absorbs or transmits: used for concentration, "
                "optical band gap (Tauc plot) and colour.",
        "short": "UV-Vis measures light absorption",
        "query": "UV-Vis",
    },
}


def glossary(user, conversation, params, text):
    terms = [t for t in (params or {}).get("terms") or [] if t in GLOSSARY]
    if not terms:
        return None
    lines = [f"**{GLOSSARY[t]['title']}** — {GLOSSARY[t]['text']}" for t in terms]
    if len(terms) >= 2:
        lines.append("**In short:** " + "; ".join(GLOSSARY[t]["short"] for t in terms) + ".")
    query = GLOSSARY[terms[0]]["query"]
    actions = [
        C.prompt_action(f"{query} instruments here", f"Which equipment can do {query}?", primary=True),
        C.prompt_action(f"Is {query} free this week?", f"Is {query} free this week?"),
        C.prompt_action(f"Charges for {query}", f"What are the charges for {query}?"),
    ]
    reply = C.reply("\n\n".join(lines), actions=actions, intent="glossary", kind="ANSWER",
                    title_hint=GLOSSARY[terms[0]]["title"].split(" (")[0])
    reply["metadata"]["source_label"] = "General science summary"
    return reply


# =============================================================================== analysis / data / software

_EXT_RE = re.compile(r"\.?\b(dm3|dm4|raw|xrdml|brml|tiff?|spc|ser|emd|xy|csv|cif|jdx|spe|vms|sp2)\b", re.IGNORECASE)
_SW_NAMES_RE = re.compile(r"\b(imagej|fiji|digital\s*micrograph|gatan|highscore|x'?pert|jade|fullprof|gsas|vesta|origin)\b", re.IGNORECASE)

_REMOTE_STEPS = [
    "1. Open the booking in **My Bookings** and press **Open Analysis Workspace** (shown when remote analysis is "
    "set up for that instrument).",
    "2. Choose your data: **Current Booking Data**, **Previous Booking Data**, or **Upload** additional files.",
    "3. Pick the software and press **Open Analysis Environment** to connect to a reserved Analysis PC. Your files "
    "appear in its **Input Data** folder.",
    "4. Save your outputs in the session workspace (Output folder). When you press **End Session**, they are "
    "uploaded to **Booking Details → Analyzed Data**, and you get an email when they are ready (files are not emailed).",
]


def _catalog_rows(eqs) -> list[tuple[str, str, list[str]]]:
    try:
        from iic_booking.remote_analysis.catalog_models import EquipmentAnalysisSoftware
    except Exception:  # noqa: BLE001
        return []
    rows = []
    seen = set()
    qs = (EquipmentAnalysisSoftware.objects.filter(equipment__in=eqs, catalog__is_active=True, catalog__is_archived=False)
          .select_related("catalog").order_by("-is_default", "sort_order", "catalog__name")[:12])
    for m in qs:
        if m.catalog_id in seen:
            continue
        seen.add(m.catalog_id)
        rows.append((m.catalog.name, (m.catalog.typical_usage or m.catalog.description or "").strip(),
                     [str(x) for x in (m.catalog.accepted_file_types or [])][:6]))
    return rows


def _catalog_search(text: str) -> list[tuple[str, str, list[str]]]:
    try:
        from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog
    except Exception:  # noqa: BLE001
        return []
    ext = _EXT_RE.search(text or "")
    name = _SW_NAMES_RE.search(text or "")
    out = []
    for c in AnalysisSoftwareCatalog.objects.filter(is_active=True, is_archived=False).order_by("name")[:60]:
        types = [str(x).lower().lstrip(".") for x in (c.accepted_file_types or [])]
        hit = (ext and ext.group(1).lower() in types) or (name and name.group(1).lower().replace(" ", "") in c.name.lower().replace(" ", ""))
        if (ext or name) and not hit:
            continue
        out.append((c.name, (c.typical_usage or c.description or "").strip(), [str(x) for x in (c.accepted_file_types or [])][:6]))
    return out[:12]


def _software_lines(rows) -> list[str]:
    out = []
    for name, usage, types in rows:
        line = f"- **{name}**"
        if usage:
            line += f" — {usage[:140]}"
        if types:
            line += f" (files: {', '.join(types)})"
        out.append(line)
    return out


def _general_software_note(text: str, query: str) -> str:
    lower = (text or "").lower()
    if re.search(r"\bdm[34]\b", lower) or "tem" in query.lower():
        return ("Generally, TEM .dm3/.dm4 files open in Gatan DigitalMicrograph, or in the free ImageJ/Fiji, which "
                "reads them directly; ImageJ is also commonly used for particle-size measurements.")
    if re.search(r"\b(p?xrd|diffraction)\b", lower) or "xrd" in query.lower():
        return ("Generally, XRD patterns are analysed with the instrument vendor's software (for example HighScore or "
                "DIFFRAC.EVA) for phase matching, and with free tools such as Profex/BGMN, GSAS-II or FullProf for "
                "Rietveld refinement.")
    return ""


def analysis_help(user, conversation, params, text):
    topic = (params or {}).get("topic") or "remote"
    eq, cands = resolve_equipment(user, conversation, text, use_context=topic == "software")
    eqs = [eq] if eq is not None else cands
    name = eq.name if eq is not None else (matching.equipment_phrase(text).upper() if cands else "")
    lines: list[str] = []
    actions: list[dict[str, Any]] = []

    if topic == "software":
        rows = _catalog_rows(eqs) if eqs else []
        if rows:
            lines.append(f"**Analysis software set up for {name or 'this instrument'}:**")
        else:
            rows = _catalog_search(text)
            if rows:
                lines.append("**Analysis software available on the portal's Analysis PCs:**")
        if rows:
            lines += _software_lines(rows)
            lines += ["", "You use it through **Remote Analysis** on your booking:"] + _REMOTE_STEPS[:3]
        else:
            lines.append("The lab hasn't published a software list for that on the portal yet, so I can't tell you "
                         "what is installed for it. The Analysis Workspace of your booking shows the software you can use.")
            note = _general_software_note(text, name)
            if note:
                lines += ["", note]
        title = "Analysis software"
    elif topic == "files":
        lines += [
            "**Where your data is**",
            "- **Raw Data** and **Analyzed Data** for each booking are on its **Booking Details** in My Bookings; "
            "**View results** lists everything uploaded for you, newest first.",
            "- The lab uploads results after your measurement and you are notified by email (download from the portal; "
            "files are not emailed).",
            "- Files you create in a remote-analysis session are uploaded to **Booking Details → Analyzed Data** when "
            "you press **End Session**.",
            "- Data a colleague shared with you is under **Shared with me** (or in My Research).",
        ]
        from iic_booking.research_copilot.services.assistant import bookings as B
        from iic_booking.research_copilot.services.booking_refs import display_ref

        recent = list(B._base_qs(user).filter(status__in=B.RESULT_STATUSES).order_by("-pk")[:3])
        if recent:
            lines += ["", "Your recent bookings:"]
            for b in recent:
                ready = B.eligibility(b)["results"]
                lines.append(f"- {display_ref(b)} {b.equipment.name}: " + ("results available" if ready else "not uploaded yet"))
                if ready and len(actions) < 2:
                    chip = B.chip(b, "results")
                    chip["label"] = f"Results {display_ref(b)}"
                    actions.append(chip)
        actions.append(C.link("View results", "/my-results", primary=not actions))
        title = "Your data"
    else:
        if eq is not None and not _catalog_rows([eq]):
            lines.append(f"Remote analysis isn't set up for **{eq.name}** on the portal yet; ask its operator how to "
                         "get your data analysed.")
            lines.append("")
        lines.append("**Analyse your data remotely**")
        lines += _REMOTE_STEPS
        lines.append("\nAnalysis time is charged separately; see **Analysis charges**.")
        title = "Remote analysis"

    actions += [C.link("My Bookings", "/my-bookings", primary=not actions), C.link("Analysis charges", "/analysis-charges")]
    if eq is not None:
        _remember(conversation, eq)
    return C.reply("\n".join(lines), actions=actions[:4], intent=f"analysis_{topic}", kind="ANSWER", title_hint=title,
                   extra={"equipment_id": int(eq.pk)} if eq is not None else None)


# =============================================================================== lab hours


def lab_hours(user, conversation, params, text):
    eq, _ = resolve_equipment(user, conversation, text, use_context=False)
    lines = [
        "There isn't one set of opening hours for all labs: each instrument has its own slot timings, set by its lab.",
        "- You can use an instrument during **your booked slot**; the booking page and the **Availability** page show "
        "each instrument's free slots.",
        "- Weekends, institute holidays and maintenance days are blocked automatically.",
        "- Sample drop-off and collection follow the instrument's sample-submission and collection rules (see "
        "**Booking rules** for the instrument).",
        "- For access outside your slot, ask the instrument's operator or Officer in Charge.",
    ]
    actions = [C.link("Availability calendar", "/availability", primary=True)]
    if eq is not None:
        minutes = int(getattr(eq, "slot_duration_minutes", 0) or 0)
        if minutes:
            lines.insert(1, f"**{eq.name}** books in {minutes}-minute slots.")
        actions = _eq_chips(eq, skip=("book", "charges")) + [
            C.assistant_action("Contacts", "ba_info", {"equipment_id": int(eq.pk), "topic": "contacts"},
                               utterance=f"Contacts for {eq.name}"),
            C.assistant_action("Booking rules", "ba_info", {"equipment_id": int(eq.pk), "topic": "rules"},
                               utterance=f"Booking rules for {eq.name}"),
        ]
        _remember(conversation, eq)
    else:
        actions += [C.prompt_action("Who runs an instrument?", "Who is the operator for FESEM?"), C.flow_action("Book equipment", "start")]
    return C.reply("\n".join(lines), actions=actions, intent="lab_hours", kind="ANSWER", title_hint="Lab access hours")


# =============================================================================== sample submission / preparation

_PREP_TIPS = {
    "xrd": "Grind powders finely and evenly (no lumps) and bring enough to fill the holder; thin films should be on a "
           "flat substrate. Mention air- or moisture-sensitive samples.",
    "sem": "Samples must be completely dry and vacuum-safe. Non-conductive samples usually need a thin gold or carbon "
           "coating; declare magnetic samples.",
    "tem": "Disperse particles (for example by sonicating in ethanol) and drop-cast on a TEM grid; areas of interest "
           "should be thinner than about 100 nm.",
    "xps": "Dry, vacuum-compatible, with a clean surface; don't touch the analysis surface.",
    "bet": "Bring a dry powder of known mass and tell the lab the highest degassing temperature it can tolerate.",
}
_TECH_GROUP = {"xrd": "xrd", "pxrd": "xrd", "gixrd": "xrd", "sem": "sem", "fesem": "sem", "eds": "sem", "tem": "tem",
               "xps": "xps", "bet": "bet"}


def _tip_for(eq, text: str) -> str:
    keys = set(matching.techniques_in(f"{getattr(eq, 'name', '')} {getattr(eq, 'code', '') or ''} {text}"))
    for k in ("fesem", "sem", "tem", "pxrd", "xrd", "xps", "bet", "eds"):
        if k in keys and _TECH_GROUP.get(k) in _PREP_TIPS:
            return _PREP_TIPS[_TECH_GROUP[k]]
    return ""


def sample_submission(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant import info

    eq, _ = resolve_equipment(user, conversation, text, use_context=False)
    out = info.policy_reply(user, "sample_submission", text, eq)
    if eq is not None:
        out["suggested_actions"] = _eq_chips(eq, skip=("book",))[:2] + out["suggested_actions"]
        _remember(conversation, eq)
    return out


def prepare(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant import info
    from iic_booking.research_copilot.services.assistant.bookings import _row

    with_cost = bool((params or {}).get("with_cost"))
    eq, cands = resolve_equipment(user, conversation, text, use_context=False)
    booking = _upcoming(user, eq) if eq is not None else None
    if eq is None and not cands:
        booking = _upcoming(user)
        eq = booking.equipment if booking is not None else None
        if eq is None:
            eq, _ = resolve_equipment(user, conversation, "", use_context=True)
    elif eq is None and cands:
        booking = next((b for b in (_upcoming(user, c) for c in cands) if b is not None), None)
        eq = booking.equipment if booking is not None else None

    if eq is None:
        lines = [
            "**Before any booking**",
            "- Check the instrument's **sample instructions** and **booking inputs** (I can show them for any instrument).",
            "- Submit your sample before the instrument's submission deadline (24 hours before the slot unless the lab "
            "set another value; earlier if that falls on a weekend or holiday).",
            "- Label samples clearly and declare anything hazardous, air-sensitive or magnetic.",
            "",
            "Which instrument is it for? For example: \"How do I prepare a sample for FESEM?\"",
        ]
        actions = [C.prompt_action("Prepare for XRD", "How should I prepare an XRD sample?"),
                   C.prompt_action("Prepare for FESEM", "How do I prepare a sample for FESEM?"),
                   C.prompt_action("My upcoming bookings", "Show my upcoming bookings")]
        return C.reply("\n".join(lines), actions=actions, intent="prepare", kind="ANSWER", title_hint="Sample preparation")

    lines = [f"**Before your {eq.name} booking**"]
    if booking is not None:
        row = _row(booking)
        lines.append(f"Your booking {row['reference']} is on **{row['when']}**.")
        if with_cost and row.get("charge") is not None:
            lines.append(f"It is charged **₹{row['charge']:,.2f}** (already debited when you booked).")
    elif with_cost:
        est = info.charges(user, eq)
        if est.get("charge") is not None:
            lines.append(f"Estimated charge for 1 sample with default inputs: **₹{est['total'] or est['charge']:,.2f}**.")
    instr = info.instructions(user, eq, limit=700)
    lines += ["", "**Lab instructions**", instr if instr else "The lab has not added special sample instructions on the portal."]
    fields = [f for f in info.input_fields(user, eq) if f["required"]][:6]
    if fields:
        lines += ["", "**Have these details ready for the booking form:** " + ", ".join(f["label"] for f in fields) + "."]
    lines += ["", "**Sample submission**"] + [f"- {x}" for x in info._builtin_policy("sample_submission", eq)]
    tip = _tip_for(eq, text)
    if tip:
        lines += ["", f"**General tip:** {tip} Follow the lab's instructions above if they differ."]
    actions = [
        C.assistant_action("Contacts", "ba_info", {"equipment_id": int(eq.pk), "topic": "contacts"}, utterance=f"Contacts for {eq.name}"),
        C.assistant_action("Booking rules", "ba_info", {"equipment_id": int(eq.pk), "topic": "rules"}, utterance=f"Booking rules for {eq.name}"),
    ]
    if booking is not None:
        actions.insert(0, C.link("Open my booking", f"/my-bookings?booking={booking.pk}", primary=True))
    else:
        actions = _eq_chips(eq, skip=("slots",))[:2] + actions
    _remember(conversation, eq)
    return C.reply("\n".join(lines), actions=actions[:4], intent="prepare", kind="ANSWER",
                   title_hint=f"Preparing for {eq.name}", extra={"equipment_id": int(eq.pk)})


# =============================================================================== cost estimate / who pays


def _estimate_block(user, eq, samples: int) -> tuple[list[str], float | None]:
    from iic_booking.research_copilot.services.intelligence import flows

    try:
        data = flows.estimate(user, eq, samples)
    except Exception:  # noqa: BLE001
        data = None
    lines, card = flows._estimate_lines(user, eq, samples, data)
    return lines, card.get("total")


def estimate(user, conversation, params, text):
    samples = (params or {}).get("samples")
    n = int(samples or 1)
    eq, cands = resolve_equipment(user, conversation, text)
    if eq is None and not cands:
        booking = _upcoming(user) if re.search(r"\bmy\b", (text or "").lower()) else None
        if booking is not None and getattr(booking, "total_charge", None) is not None:
            from iic_booking.research_copilot.services.assistant.bookings import _row

            row = _row(booking)
            return C.reply(
                f"Your next booking {row['reference']} ({row['equipment']}, {row['when']}) is charged "
                f"**₹{row['charge']:,.2f}**.",
                actions=[C.link("Open my booking", f"/my-bookings?booking={booking.pk}", primary=True)]
                + _eq_chips(booking.equipment, skip=("book", "slots")),
                intent="estimate", title_hint="Booking charge",
            )
        return C.reply(
            "Which instrument is it for? Tell me the equipment and the number of samples, for example "
            "\"How much do 5 XRD samples cost?\", or open the Analysis Charges list.",
            actions=[C.prompt_action("5 XRD samples", "How much do 5 XRD samples cost?"),
                     C.prompt_action("FESEM cost", "How much does a FESEM booking cost?"),
                     C.link("Analysis charges", "/analysis-charges")],
            intent="estimate", kind="CLARIFICATION", title_hint="Cost estimate",
        )
    eqs = [eq] if eq is not None else cands
    lines: list[str] = []
    for e in eqs:
        block, _total = _estimate_block(user, e, n)
        lines += block + [""]
    head = f"Estimated cost for **{n} sample{'s' if n != 1 else ''}**" + (" (default inputs):" if not samples else ":")
    lines = [head, ""] + lines
    lines.append("This is an estimate for your account category. The booking page recalculates the exact charge from "
                 "your actual inputs before you confirm.")
    if eq is not None:
        actions = _eq_chips(eq, skip=("charges",)) + [C.link("Analysis charges", "/analysis-charges")]
        _remember(conversation, eq)
    else:
        actions = [C.flow_action(f"Book {e.name}"[:40], "equipment", {"equipment_id": int(e.pk)}) for e in cands[:2]]
        actions.append(C.link("Analysis charges", "/analysis-charges"))
    return C.reply("\n".join(lines).strip(), actions=actions[:4], intent="estimate", title_hint="Cost estimate",
                   extra={"equipment_id": int(eq.pk)} if eq is not None else None)


def who_pays(user, conversation, params, text):
    from iic_booking.research_copilot.services.assistant import daily, info

    caps = daily._caps(user)
    category = info._user_type_label(user) or "your account type"
    lines = [
        f"Charges come from the instrument's charge profile for the person making the booking (your category: "
        f"**{category}**). The booking page shows the exact amount before you confirm.",
        "- **PI rates** apply when you, or the owner of the wallet you book from, are registered as a PI of that "
        "instrument and the lab has set PI rates for it.",
        "- Otherwise the standard rate for your category applies (or a discounted rate if your account has been "
        "approved for one).",
    ]
    if caps.has_wallet:
        if caps.wallet_is_shared:
            owner = caps.wallet_owner_name or "your supervisor"
            lines.append(f"- Your bookings are debited from **{owner}**'s wallet.")
        else:
            lines.append("- Your bookings are debited from your own wallet"
                         + (", and your linked students' bookings from it too." if daily.user_type(user) == "faculty" else "."))
    eq, cands = resolve_equipment(user, conversation, text)
    eq = eq or (cands[0] if len(cands) == 1 else None)
    actions: list[dict[str, Any]] = []
    if eq is not None:
        try:
            from iic_booking.equipment.pi_pricing import resolve_pricing_profile_for_user

            profile = str(resolve_pricing_profile_for_user(user, eq) or "").upper()
        except Exception:  # noqa: BLE001
            profile = ""
        label = {"PI": "PI rates", "DISCOUNTED": "your discounted rate", "STANDARD": "the standard rate for your category"}.get(profile)
        if label:
            lines += ["", f"For **{eq.name}**, your bookings use **{label}**."]
        n = int((params or {}).get("samples") or 1)
        block, _ = _estimate_block(user, eq, n)
        lines += [""] + block
        actions = _eq_chips(eq, skip=("charges",))
        _remember(conversation, eq)
    actions += [C.prompt_action("Wallet balance", "What is my wallet balance?"), C.link("Analysis charges", "/analysis-charges")]
    return C.reply("\n".join(lines), actions=actions[:4], intent="who_pays", kind="ANSWER", title_hint="Who pays")


HANDLERS = {
    "smalltalk": smalltalk,
    "vague": vague,
    "find_equipment": find_equipment,
    "glossary": glossary,
    "lab_hours": lab_hours,
    "who_pays": who_pays,
    "prepare": prepare,
    "sample_submission": sample_submission,
    "analysis_help": analysis_help,
    "estimate": estimate,
}
