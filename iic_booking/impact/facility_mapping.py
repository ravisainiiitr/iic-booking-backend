"""Publication-to-facility mapping: read a paper's legal open-access copy, find its acknowledgement, and match it to the
departments/centres and equipment listed on the portal.

Only open-access copies that OpenAlex lists (publisher OA, repositories, PubMed Central) are read; nothing behind a
subscription. Papers without one keep their PublicationSuggestion untouched for later. Per paper and researcher one
PublicationEvidence row records the source URL, a short acknowledgement excerpt and the outcome; each facility it names
becomes a PublicationFacilityMapping with the matched phrases, the rationale and a confidence score.

Mappings at or above ``PUBLICATION_MAPPING_CLAIM_THRESHOLD`` are SUGGESTED and the paper waits (review_status PENDING)
for the Main Administrator, or the Department Administrator of the department it names (see match_review). Nothing is
published until one of them approves; approval creates the usual approved publication.
"""

from __future__ import annotations

import html
import io
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    AliasSource,
    EvidenceStatus,
    FacilityAlias,
    MappingLevel,
    MappingStatus,
    PublicationEvidence,
    PublicationFacilityMapping,
    PublicationSuggestion,
    SuggestionStatus,
)

logger = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/works"
USER_AGENT = "facility-booking-portal (publication acknowledgement check)"
DOI_BATCH = 40
MAX_PAGES_READ = 80
ACK_MAX_CHARS = 3000
SNIPPET_MAX_CHARS = 600
WORK_SELECT = "id,doi,ids,title,type,publication_year,primary_location,authorships,open_access,best_oa_location,locations"
EUROPE_PMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
DOWNLOAD_SECONDS = 60  # a whole download, however slowly the server sends it
MIN_TEXT_CHARS = 2000  # less than this is a cover page, a login wall or a bot check, not the paper
# Pages publishers serve to automated clients instead of the paper. They are reported, never worked around.
BLOCK_PAGE = re.compile(r"(?i)unable to complete the above request|verify you are (a )?human|captcha|"
                        r"enable javascript and cookies|access denied|just a moment\.\.\.|incident id")

# Confidence by alias source (department and equipment); see score_matches().
DEPARTMENT_CONFIDENCE = {AliasSource.MANUAL: 0.95, AliasSource.NAME: 0.95, AliasSource.POLICY: 0.95,
                         AliasSource.DERIVED: 0.95, AliasSource.CODE: 0.75}
EQUIPMENT_CONFIDENCE = {
    AliasSource.MANUAL: 0.9, AliasSource.NAME: 0.9, AliasSource.CODE: 0.6, AliasSource.DERIVED: 0.6,
    AliasSource.CATEGORY: 0.5, AliasSource.FUNDING: 0.3,
}
# Instrument-name prefixes that qualify a technique ("FE-SEM" is an SEM, "HR-TEM" a TEM, "PXRD" an XRD).
TECHNIQUE_PREFIXES = ("FEG", "FE", "HR", "P")
ACRONYM_IN_BRACKETS = re.compile(r"\(([A-Za-z][A-Za-z0-9-]{1,11})\)")
# The model written after the bracketed acronym: "Field Emission Scanning Electron Microscope (FESEM)-GEMINI 300".
MODEL_AFTER_BRACKETS = re.compile(r"\)\s*[-\u2013]\s*([A-Za-z][A-Za-z0-9 ]{2,30})\s*$")
DEPARTMENT_BOOST = 0.2  # equipment match whose own department is also acknowledged
EQUIPMENT_WITHOUT_DEPARTMENT = 0.6  # cap when the equipment's department/centre is not named
AMBIGUOUS_FACTOR = 0.6  # the same phrase matched several equipment items
ONE_WORD_DEPARTMENT = 0.75  # "Chemistry" also names other institutes' departments
NO_INSTITUTE_FACTOR = 0.5  # no author at the institute and the acknowledgement does not name it

GENERIC_WORDS = {"test", "sample", "general", "other", "equipment", "instrument", "facility", "lab", "laboratory",
                 "ultra", "pro", "plus", "max", "mini", "lite", "new", "old"}
AUTO_CODE_RE = re.compile(r"^[A-Z]{1,4}-\d{8,}", re.I)  # generated codes like EQ-20260730021949-A6BB50


# --------------------------------------------------------------------------- aliases
def _alternatives(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split("|") if p.strip()]


def _good_code(code: str) -> bool:
    code = (code or "").strip()
    return (3 <= len(code) <= 12 and any(c.isalpha() for c in code) and not AUTO_CODE_RE.match(code)
            and code.lower() not in GENERIC_WORDS)


def _good_name(name: str) -> bool:
    name = (name or "").strip()
    return len(name) >= 6 and not AUTO_CODE_RE.match(name) and name.lower() not in GENERIC_WORDS and "-updated" not in name.lower()


def name_variants(name: str) -> list[str]:
    """Spelling variants of a department/centre name: Centre <-> Center."""
    out = []
    for a, b in (("Centre", "Center"), ("Center", "Centre"), ("centre", "center"), ("center", "centre")):
        if re.search(rf"\b{a}\b", name):
            out.append(re.sub(rf"\b{a}\b", b, name))
    return out


def acronym_variants(token: str) -> list[str]:
    """For an instrument acronym or code: hyphen/space variants and the base technique.
    "FE-SEM" -> FESEM, FE SEM, SEM;  "FESEM" -> FE-SEM, SEM;  "HRTEM" -> HR-TEM, TEM;  "PXRD" -> P-XRD, XRD."""
    t = token.strip()
    if not t or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,14}", t):
        return []
    out: list[str] = []
    singles = [t]
    if "-" in t:
        parts = [p for p in t.split("-") if p]
        out += ["".join(parts), " ".join(parts)]
        base = parts[-1]
        if len(base) >= 3 and base.isalpha():
            out.append(base)
        singles = [p for p in parts if len(p) >= 4]  # "ICPMS-MS" -> also look at "ICPMS"
    for one in singles:
        if "-" in one or not one.isupper():
            continue
        for prefix in TECHNIQUE_PREFIXES:
            rest = one[len(prefix):]
            if one.startswith(prefix) and len(rest) >= 3 and rest.isalpha():
                out += [f"{prefix}-{rest}", rest]
                break
        if len(one) >= 4 and one.endswith("MS") and one[:-2].isalpha():
            out += [f"{one[:-2]}-MS", one]  # mass spectrometry: "ICPMS" is written "ICP-MS"
    return [v for v in dict.fromkeys(out) if v.lower() != t.lower()]


def generate_aliases() -> list[dict]:
    """Aliases derived from what the portal already holds (name, code, category, policy and funding phrases)."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models import Department

    from .models import AcknowledgementPolicy, EquipmentFunding

    out: list[dict] = []
    seen: set[tuple] = set()
    dept_words = department_phrases()

    def add(dept_id, eq_id, phrase, source, case_sensitive=False):
        phrase = re.sub(r"\s+", " ", (phrase or "").strip())[:200]
        key = (dept_id, eq_id, phrase.lower())
        if not phrase or key in seen:
            return
        if eq_id is not None and phrase.lower() in dept_words:
            return  # "IIC" from "Electronic Repair lab (IIC)" names the centre, not this instrument
        seen.add(key)
        out.append({"department_id": dept_id, "equipment_id": eq_id, "phrase": phrase, "source": source,
                    "case_sensitive": case_sensitive})

    eq_rows = list(Equipment.objects.filter(internal_department__isnull=False)
                   .values("equipment_id", "name", "code", "internal_department_id", "category__name"))
    host_ids = {r["internal_department_id"] for r in eq_rows}
    depts = list(Department.objects.filter(pk__in=host_ids).values("id", "name", "code"))
    by_text: dict[str, int] = {}
    for d in depts:
        if _good_name(d["name"]):
            add(d["id"], None, d["name"], AliasSource.NAME)
            by_text[d["name"].strip().lower()] = d["id"]
            for v in name_variants(d["name"].strip()):
                add(d["id"], None, v, AliasSource.DERIVED)
        if _good_code(d["code"] or ""):
            add(d["id"], None, d["code"], AliasSource.CODE, case_sensitive=True)
            by_text[(d["code"] or "").strip().lower()] = d["id"]

    for r in eq_rows:
        eid, name, code = r["equipment_id"], (r["name"] or "").strip(), (r["code"] or "").strip()
        if _good_name(name) and name.lower() != code.lower():
            add(None, eid, name, AliasSource.NAME)
            bare = name.split("(", 1)[0].strip(" -") if ACRONYM_IN_BRACKETS.search(name) else name
            if bare != name and _good_name(bare):
                add(None, eid, bare, AliasSource.NAME)  # "Transmission Electron Microscope"
        model = MODEL_AFTER_BRACKETS.search(name)
        if model:
            label = " ".join(model.group(1).split())
            if label.lower() not in GENERIC_WORDS and (len(label) >= 5 or " " in label):
                add(None, eid, label, AliasSource.DERIVED)  # "GEMINI 300", "APREO" (any case)
        if _good_code(code):
            add(None, eid, code, AliasSource.CODE, case_sensitive=True)
        tokens = [m.group(1) for m in ACRONYM_IN_BRACKETS.finditer(name)]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,14}", name) and name.upper() == name:
            tokens.append(name)  # a name that is itself an acronym ("FE-SEM")
        for tok in tokens:
            if _good_code(tok) or len(tok) == 3:
                add(None, eid, tok, AliasSource.DERIVED, case_sensitive=tok.upper() == tok)
        for tok in dict.fromkeys(tokens + ([code] if _good_code(code) else [])):
            for v in acronym_variants(tok):
                if len(v.replace(" ", "")) >= 3:
                    add(None, eid, v, AliasSource.DERIVED, case_sensitive=v.upper() == v)
        if r["category__name"] and len(r["category__name"].strip()) >= 5 and r["category__name"].strip().lower() not in GENERIC_WORDS:
            add(None, r["equipment_id"], r["category__name"], AliasSource.CATEGORY)

    # Policy phrases: a group of alternatives ("Institute Instrumentation Centre|IIC") naming a department.
    pol = AcknowledgementPolicy.objects.first()
    for group in (pol.required_phrases if pol else []) or []:
        alts = _alternatives(group)
        dept_id = next((by_text[a.lower()] for a in alts if a.lower() in by_text), None)
        if dept_id:
            for a in alts:
                add(dept_id, None, a, AliasSource.POLICY, case_sensitive=len(a) <= 5 and a.isupper())

    eq_ids = {r["equipment_id"] for r in eq_rows}
    for f in EquipmentFunding.objects.filter(is_active=True, equipment_id__in=eq_ids).exclude(acknowledgement_phrase=""):
        for a in _alternatives(f.acknowledgement_phrase):
            add(None, f.equipment_id, a, AliasSource.FUNDING, case_sensitive=len(a) <= 5 and a.isupper())
    return out


def department_phrases() -> set[str]:
    """Lower-cased phrases that name a department or centre: every department's name, its spelling variants and code,
    and the acknowledgement policy's phrases. An instrument never gets one of these as its own alias."""
    from iic_booking.users.models import Department

    from .models import AcknowledgementPolicy

    words: set[str] = set()
    for d in Department.objects.values("name", "code"):
        name, code = (d["name"] or "").strip(), (d["code"] or "").strip()
        if name:
            words.add(name.lower())
            words.update(v.lower() for v in name_variants(name))
        if code:
            words.add(code.lower())
    pol = AcknowledgementPolicy.objects.first()
    for group in (pol.required_phrases if pol else []) or []:
        words.update(a.lower() for a in _alternatives(group))
    return {w for w in words if w}


def department_clashes() -> list[FacilityAlias]:
    """Active generated equipment aliases whose phrase names a department (administrators' MANUAL aliases are kept)."""
    words = department_phrases()
    qs = (FacilityAlias.objects.filter(is_active=True, equipment__isnull=False).exclude(source=AliasSource.MANUAL)
          .select_related("equipment").order_by("pk"))
    return [a for a in qs if a.phrase.strip().lower() in words]


def switch_off_clashes(*, apply: bool) -> list[FacilityAlias]:
    clashes = department_clashes()
    if apply and clashes:
        FacilityAlias.objects.filter(pk__in=[a.pk for a in clashes]).update(is_active=False)
    return clashes


def seed_aliases(*, apply: bool, user=None) -> dict:
    """Store generated aliases that do not exist yet. Existing ones (including switched-off ones) are left alone."""
    created = skipped = 0
    rows = generate_aliases()
    for r in rows:
        qs = FacilityAlias.objects.filter(phrase__iexact=r["phrase"])
        qs = qs.filter(department_id=r["department_id"]) if r["department_id"] else qs.filter(equipment_id=r["equipment_id"])
        if qs.exists():
            skipped += 1
            continue
        created += 1
        if apply:
            FacilityAlias.objects.create(created_by=user, **r)
    return {"generated": len(rows), "new": created, "existing": skipped, "rows": rows}


# --------------------------------------------------------------------------- matching
@dataclass
class CompiledAlias:
    alias_id: int
    department_id: int | None
    equipment_id: int | None
    phrase: str
    source: str
    pattern: re.Pattern


def _alias_pattern(phrase: str, case_sensitive: bool) -> re.Pattern:
    body = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", 0 if case_sensitive else re.I)


def load_aliases() -> list[CompiledAlias]:
    return [
        CompiledAlias(a.pk, a.department_id, a.equipment_id, a.phrase, a.source, _alias_pattern(a.phrase, a.case_sensitive))
        for a in FacilityAlias.objects.filter(is_active=True)
    ]


@dataclass
class Match:
    level: str
    department_id: int
    equipment_id: int | None
    phrases: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    rationale: str = ""


def score_matches(text: str, aliases: list[CompiledAlias], institute_context: bool | None = True) -> list[Match]:
    """Facilities an acknowledgement names, with a confidence per facility.

    Department: 0.95 for its name / policy phrase / an administrator's alias, 0.75 for its code or a one-word name.
    Equipment: 0.9 for its name or an administrator's alias, 0.6 code, 0.5 category, 0.3 funding phrase; +0.2 when
    the equipment's own department is also acknowledged, otherwise at most 0.6 (so no claim); x0.6 when the same
    phrase matched several equipment items.
    Everything x0.5 when the paper shows no link to the institute (``institute_context`` False).
    """
    from iic_booking.equipment.models import Equipment

    hits = [(a, m) for a in aliases for m in [a.pattern.search(text)] if m]
    if not hits:
        return []
    dept: dict[int, Match] = {}
    for a, m in hits:
        if a.department_id:
            row = dept.setdefault(a.department_id, Match(MappingLevel.DEPARTMENT, a.department_id, None))
            row.phrases.append(m.group(0))
            row.sources.append(a.source)
    for row in dept.values():
        row.confidence = max(
            DEPARTMENT_CONFIDENCE.get(s, 0.75) if len(p.split()) > 1 or s == AliasSource.MANUAL else ONE_WORD_DEPARTMENT
            for s, p in zip(row.sources, row.phrases))
        row.rationale = "Acknowledgement names the department/centre: " + ", ".join(f'"{p}"' for p in row.phrases) + "."

    eq_hits = [(a, m) for a, m in hits if a.equipment_id]
    phrase_spread: dict[str, set[int]] = {}
    for a, m in eq_hits:
        phrase_spread.setdefault(a.phrase.lower(), set()).add(a.equipment_id)
    eq_dept = dict(Equipment.objects.filter(pk__in={a.equipment_id for a, _ in eq_hits})
                   .values_list("equipment_id", "internal_department_id"))
    equipment: dict[int, Match] = {}
    for a, m in eq_hits:
        dept_id = eq_dept.get(a.equipment_id)
        if not dept_id:
            continue
        row = equipment.setdefault(a.equipment_id, Match(MappingLevel.EQUIPMENT, dept_id, a.equipment_id))
        base = EQUIPMENT_CONFIDENCE.get(a.source, 0.3)
        shared = len(phrase_spread[a.phrase.lower()]) > 1
        if shared:
            base *= AMBIGUOUS_FACTOR
        row.phrases.append(m.group(0))
        row.sources.append(a.source + (" (shared)" if shared else ""))
        row.confidence = max(row.confidence, base)
    for row in equipment.values():
        reasons = [f'"{p}" ({s.lower()})' for p, s in zip(row.phrases, row.sources)]
        row.rationale = "Acknowledgement names the equipment: " + ", ".join(reasons) + "."
        if row.department_id in dept:
            row.confidence = min(0.98, row.confidence + DEPARTMENT_BOOST)
            row.rationale += " Its department/centre is acknowledged too."
        else:
            # A technique or instrument name alone ("X-ray diffractometer") also describes other labs' instruments.
            row.confidence = min(row.confidence, EQUIPMENT_WITHOUT_DEPARTMENT)
            row.rationale += " Its department/centre is not named, so this alone is not enough for a claim."
    for row in list(dept.values()) + list(equipment.values()):
        if institute_context is False:
            row.confidence *= NO_INSTITUTE_FACTOR
            row.rationale += " Neither an author affiliation nor the acknowledgement names the institute."
        elif institute_context is None:
            row.rationale += " The institute was not checked (INSTITUTE_ROR_ID / INSTITUTE_ACK_NAMES not set)."
        row.confidence = round(row.confidence, 3)
    return sorted(dept.values(), key=lambda r: -r.confidence) + sorted(equipment.values(), key=lambda r: -r.confidence)


def institute_names() -> list[re.Pattern]:
    raw = getattr(settings, "INSTITUTE_ACK_NAMES", "") or ""
    return [_alias_pattern(p, case_sensitive=p.isupper() and len(p) <= 5) for p in _alternatives(raw)]


def institute_context(work: dict | None, ack: str) -> bool | None:
    """True when an author of the paper is at the institute (ROR) or the acknowledgement names it; None when neither
    INSTITUTE_ROR_ID nor INSTITUTE_ACK_NAMES is configured."""
    from .author_linking import institute_ror

    ror, names = institute_ror(), institute_names()
    if not ror and not names:
        return None
    if ror:
        for a in (work or {}).get("authorships") or []:
            if any((i or {}).get("ror") == ror for i in a.get("institutions") or []):
                return True
    return any(p.search(ack) for p in names)


# --------------------------------------------------------------------------- acknowledgement extraction
ACK_HEADING = re.compile(r"(?im)(?:^|\n|\.\s)\s*(?:\d+\.?\s*)?acknowled?ge?ments?\b\s*[:.\-–—]?\s*")
ACK_END = re.compile(
    r"(?im)(?:^|\n)\s*(?:\d+\.?\s*)?(?:references|bibliography|notes and references|literature cited|"
    r"conflicts? of interest|declaration of competing interest|competing interests?|declarations?|"
    r"author contributions?|credit authorship|data availability|appendix|supplementary (?:material|information)|"
    r"orcid|ethics statement|disclosure statement)\b"
)
THANKS = re.compile(r"\b(thank|grateful|gratitude|acknowledg|indebted)", re.I)


LINE_NUMBER = re.compile(r"(?m)^[ \t]*\d{1,4}[ \t]+")


def normalise_text(text: str) -> str:
    text = (text or "").replace("\r", "\n").replace("­", "")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    numbered = sum(1 for ln in lines if LINE_NUMBER.match(ln))
    if numbered >= 20 and numbered > 0.3 * len(lines):
        text = LINE_NUMBER.sub("", text)  # preprints with numbered lines
    text = re.sub(r"(\w)-\n\s*([A-Z0-9])", r"\1-\2", text)  # "ICP-" then "MS": a real hyphen
    text = re.sub(r"(\w)-\n\s*(\w)", r"\1\2", text)  # a word hyphenated across lines
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_acknowledgement(text: str) -> tuple[str, str]:
    """(acknowledgement text, how it was found). Prefers the last "Acknowledgements" heading that is followed by
    thank-you wording; otherwise collects thank-you sentences from the second half of the paper."""
    text = normalise_text(text)
    best = ""
    for m in ACK_HEADING.finditer(text):
        body = text[m.end():m.end() + ACK_MAX_CHARS * 2]
        end = ACK_END.search(body)
        body = body[:end.start()] if end else body
        body = body[:ACK_MAX_CHARS].strip()
        if THANKS.search(body[:800]) or not best:
            best = body
    if best:
        return re.sub(r"\s+", " ", best), "acknowledgement heading"
    tail = text[len(text) // 2:]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", tail) if THANKS.search(s)]
    joined = re.sub(r"\s+", " ", " ".join(sentences))[:ACK_MAX_CHARS]
    return (joined, "thank-you sentences (no heading)") if joined else ("", "")


def snippet_for(ack: str, phrases: list[str]) -> str:
    """The sentences of the acknowledgement that contain a matched phrase, cut to a short excerpt."""
    sentences = re.split(r"(?<=[.!?])\s+", ack)
    keep = [s for s in sentences if any(p.lower() in s.lower() for p in phrases)] or sentences[:2]
    out = " ".join(keep)
    return out if len(out) <= SNIPPET_MAX_CHARS else out[:SNIPPET_MAX_CHARS - 1] + "…"


# --------------------------------------------------------------------------- open-access copies
class _TextFromHtml(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg"}
    BLOCK = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextFromHtml()
    parser.feed(raw)
    return html.unescape("".join(parser.parts))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:MAX_PAGES_READ]
    return "\n".join((p.extract_text() or "") for p in pages)


def _download(url: str, accept: str = "application/pdf,text/html;q=0.9,*/*;q=0.5") -> tuple[bytes, str]:
    limit = int(getattr(settings, "PUBLICATION_MAPPING_MAX_PDF_BYTES", 30 * 1024 * 1024))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    started = time.monotonic()
    chunks, size = [], 0
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - URLs come from OpenAlex OA locations
        ctype = (resp.headers.get("Content-Type") or "").lower()
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError(f"larger than {limit} bytes")
            if time.monotonic() - started > DOWNLOAD_SECONDS:
                raise TimeoutError(f"download took longer than {DOWNLOAD_SECONDS}s")
            chunks.append(chunk)
    return b"".join(chunks), ctype


def jats_to_text(xml: str) -> str:
    """Plain text from JATS XML (Europe PMC), keeping section titles on their own lines."""
    xml = re.sub(r"(?is)<(ref-list|table-wrap|fig)\b.*?</\1>", "\n", xml)
    xml = re.sub(r"(?i)</?(title|p|sec|ack|back|body|abstract|list-item)\b[^>]*>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", " ", xml))


def read_full_text(url: str) -> str:
    if url.startswith("https://www.ebi.ac.uk/europepmc/"):
        data, _ = _download(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.5")
        return jats_to_text(data.decode("utf-8", errors="replace")) + "\nReferences\n"
    data, ctype = _download(url)
    if data[:5] == b"%PDF-" or "pdf" in ctype:
        return pdf_to_text(data)
    if "html" in ctype or data.lstrip()[:1] == b"<":
        return html_to_text(data.decode("utf-8", errors="replace"))
    raise ValueError(f"unsupported content type {ctype or 'unknown'}")


def open_access_urls(work: dict) -> list[tuple[str, str, str]]:
    """(url, oa_status, licence) of the work's legal open-access copies: PDFs first, then landing pages."""
    oa_status = ((work.get("open_access") or {}).get("oa_status") or "")
    locations = [work.get("best_oa_location")] + list(work.get("locations") or [])
    pdfs, pages = [], []
    pmcid = str(((work.get("ids") or {}).get("pmcid") or "")).rstrip("/").rsplit("/", 1)[-1]
    if pmcid and (work.get("open_access") or {}).get("is_oa"):
        pmcid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
        pdfs.append((EUROPE_PMC_XML.format(pmcid=pmcid), oa_status, "Europe PMC open access"))
    for loc in locations:
        if not loc or not loc.get("is_oa"):
            continue
        lic = loc.get("license") or ""
        if loc.get("pdf_url"):
            pdfs.append((loc["pdf_url"], oa_status, lic))
        if loc.get("landing_page_url"):
            pages.append((loc["landing_page_url"], oa_status, lic))
    out, seen = [], set()
    for row in pdfs + pages:
        if row[0] not in seen:
            seen.add(row[0])
            out.append(row)
    return out


def openalex_works_by_doi(dois: list[str]) -> dict[str, dict]:
    from iic_booking.equipment.publication_dedupe import normalize_doi

    from . import openalex

    found: dict[str, dict] = {}
    for i in range(0, len(dois), DOI_BATCH):
        params = {"filter": "doi:" + "|".join(dois[i:i + DOI_BATCH]), "per-page": "50", "select": WORK_SELECT}
        body = openalex.get_json(f"{OPENALEX_URL}?{urllib.parse.urlencode(params)}", user_agent=USER_AGENT)
        for w in body.get("results") or []:
            found[normalize_doi(w.get("doi") or "")] = w
    return found


# --------------------------------------------------------------------------- one paper
@dataclass
class PaperResult:
    doi: str
    title: str
    year: int | None
    status: str
    source_url: str = ""
    oa_status: str = ""
    licence: str = ""
    snippet: str = ""
    detail: str = ""
    matches: list[Match] = field(default_factory=list)


def check_paper(doi: str, title: str, year, work: dict | None, aliases: list[CompiledAlias], reader=None) -> PaperResult:
    reader = reader or read_full_text
    res = PaperResult(doi=doi, title=title, year=year, status=EvidenceStatus.NO_OPEN_ACCESS)
    urls = open_access_urls(work or {})
    if not urls:
        res.detail = "OpenAlex lists no open-access copy." if work else "Not found in OpenAlex."
        return res
    errors = []
    for url, oa_status, lic in urls[:4]:
        try:
            text = reader(url)
        except Exception as exc:  # network, HTTP, parsing
            code = getattr(exc, "code", None)
            errors.append(f"{urllib.parse.urlparse(url).netloc}: {type(exc).__name__}{f' {code}' if code else ''}")
            continue
        host = urllib.parse.urlparse(url).netloc
        if len(text.strip()) < MIN_TEXT_CHARS:
            reason = "the site blocked automated access" if BLOCK_PAGE.search(text) else "too little text"
            errors.append(f"{host}: {reason}")
            continue
        if BLOCK_PAGE.search(text[:3000]) and not ACK_HEADING.search(text):
            errors.append(f"{host}: the site blocked automated access")
            continue
        res.source_url, res.oa_status, res.licence = url, oa_status, lic
        ack, how = extract_acknowledgement(text)
        if not ack:
            res.status, res.detail = EvidenceStatus.NO_ACKNOWLEDGEMENT, "No acknowledgement found in the open-access text."
            return res
        res.matches = score_matches(ack, aliases, institute_context(work, ack))
        phrases = [p for m in res.matches for p in m.phrases]
        res.snippet = snippet_for(ack, phrases) if phrases else snippet_for(ack, [])[:300]
        res.status = EvidenceStatus.MATCHED if res.matches else EvidenceStatus.NO_MATCH
        res.detail = f"Acknowledgement found by {how}."
        return res
    res.status, res.detail = EvidenceStatus.FETCH_FAILED, ("; ".join(errors))[:500]
    return res


# --------------------------------------------------------------------------- saving
def claim_threshold() -> float:
    return float(getattr(settings, "PUBLICATION_MAPPING_CLAIM_THRESHOLD", 0.7))


def _existing_equipment_publication(equipment_id: int, doi: str) -> bool:
    from iic_booking.equipment.models import EquipmentPublication

    return bool(doi) and EquipmentPublication.objects.filter(equipment_id=equipment_id, doi__iexact=doi).exists()


def save_result(user, res: PaperResult, work: dict | None, suggestion=None) -> dict:
    """Store the evidence and mappings. A paper with a probable match (any mapping at or above the threshold, on
    equipment or a department that does not already list it) waits for review. A decision already taken (approved
    or rejected) is never reopened by a later check."""
    from .models import ReviewStatus

    threshold = claim_threshold()
    out = {"queued": False, "mappings": 0, "evidence_id": None}
    with transaction.atomic():
        lookup = {"user": user, "doi": res.doi} if res.doi else {"user": user, "openalex_id": (work or {}).get("id") or "", "title": res.title[:500]}
        ev = PublicationEvidence.objects.select_for_update().filter(**lookup).first()
        if ev and ev.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
            out["evidence_id"] = ev.pk
            return out
        fields = {
            "suggestion": suggestion, "openalex_id": (work or {}).get("id") or "", "title": res.title[:500],
            "year": res.year, "status": res.status, "source_url": res.source_url[:1000], "oa_status": res.oa_status[:20],
            "licence": (res.licence or "")[:60], "acknowledgement_snippet": res.snippet, "detail": res.detail[:500],
            "checked_at": timezone.now(),
        }
        if ev is None:
            ev = PublicationEvidence.objects.create(**lookup, **fields)
        else:
            for k, v in fields.items():
                setattr(ev, k, v)
            ev.save()
        suggested = _store_mappings(ev, res.matches, res.doi, threshold)
        out["mappings"] = len(res.matches)
        ev.review_status = ReviewStatus.PENDING if suggested else ReviewStatus.NONE
        ev.save()
        out.update(queued=bool(suggested), evidence_id=ev.pk)
    return out


def _store_mappings(ev, matches: list[Match], doi: str, threshold: float) -> int:
    """Make the paper's mappings exactly this check's matches (ones no longer found are removed); returns how many
    reach the review threshold."""
    suggested = 0
    keep: list[int] = []
    for m in matches:
        listed = m.level == MappingLevel.EQUIPMENT and _existing_equipment_publication(m.equipment_id, doi)
        status = MappingStatus.SUGGESTED if m.confidence >= threshold and not listed else MappingStatus.BELOW_THRESHOLD
        suggested += status == MappingStatus.SUGGESTED
        key = {"evidence": ev, "level": m.level, "department_id": m.department_id, "equipment_id": m.equipment_id}
        row, _ = PublicationFacilityMapping.objects.update_or_create(**key, defaults={
            "matched_phrases": m.phrases, "rationale": m.rationale + (" The equipment already lists this paper." if listed else ""),
            "confidence": Decimal(str(m.confidence)), "status": status,
        })
        keep.append(row.pk)
    ev.mappings.exclude(pk__in=keep).delete()
    return suggested


def rescore_pending(*, apply: bool, skip_alias_ids: set[int] | frozenset = frozenset()) -> list[dict]:
    """Re-match papers waiting for review against the current aliases, from their stored acknowledgement (no download).
    Waiting papers already passed the institute check, so it is not repeated. Decided papers are never touched; a
    paper left with nothing at the threshold leaves the queue."""
    from .models import ReviewStatus

    aliases = [a for a in load_aliases() if a.alias_id not in skip_alias_ids]
    threshold, changes = claim_threshold(), []
    for ev in PublicationEvidence.objects.filter(review_status=ReviewStatus.PENDING).order_by("pk"):
        matches = score_matches(ev.acknowledgement_snippet or "", aliases, True)
        before = {(m.level, m.department_id, m.equipment_id) for m in ev.mappings.all()}
        after = {(m.level, m.department_id, m.equipment_id) for m in matches}
        if before == after:
            continue
        change = {"evidence_id": ev.pk, "removed": sorted(before - after, key=str), "added": sorted(after - before, key=str)}
        if apply:
            with transaction.atomic():
                ev = PublicationEvidence.objects.select_for_update().get(pk=ev.pk)
                if ev.review_status != ReviewStatus.PENDING:
                    continue
                suggested = _store_mappings(ev, matches, ev.doi, threshold)
                if not suggested:
                    ev.review_status = ReviewStatus.NONE
                    ev.save(update_fields=["review_status"])
                change["still_pending"] = bool(suggested)
        changes.append(change)
    return changes


# --------------------------------------------------------------------------- a researcher's suggestions
def verify_user(user, *, apply: bool, dois: list[str] | None = None, limit: int | None = None, recheck: bool = False,
                reader=None, works_lookup=None, log=None) -> dict:
    """Check the open-access acknowledgement of the user's suggested papers (dismissed ones skipped; suggestions are
    never changed). Dry run unless ``apply``."""
    from iic_booking.equipment.publication_dedupe import normalize_doi

    works_lookup = works_lookup or openalex_works_by_doi
    qs = PublicationSuggestion.objects.filter(user=user).exclude(status=SuggestionStatus.DISMISSED).exclude(doi="")
    if dois:
        qs = qs.filter(doi__in=[normalize_doi(d) for d in dois])
    if not recheck:
        qs = qs.exclude(doi__in=PublicationEvidence.objects.filter(user=user).values_list("doi", flat=True))
    rows = list(qs.order_by("-year", "pk")[:limit] if limit else qs.order_by("-year", "pk"))
    works = works_lookup([r.doi for r in rows]) if rows else {}
    aliases = load_aliases()
    summary = {"papers": len(rows), "by_status": {}, "department_mappings": 0, "equipment_mappings": 0,
               "for_review": 0, "queued": 0, "queued_ids": [], "results": []}
    threshold = claim_threshold()
    for s in rows:
        work = works.get(s.doi)
        res = check_paper(s.doi, s.title, s.year, work, aliases, reader=reader)
        summary["by_status"][res.status] = summary["by_status"].get(res.status, 0) + 1
        summary["department_mappings"] += sum(1 for m in res.matches if m.level == MappingLevel.DEPARTMENT)
        summary["equipment_mappings"] += sum(1 for m in res.matches if m.level == MappingLevel.EQUIPMENT)
        if any(m.confidence >= threshold and not (m.level == MappingLevel.EQUIPMENT
                                                  and _existing_equipment_publication(m.equipment_id, res.doi))
               for m in res.matches):
            summary["for_review"] += 1
        if apply:
            done = save_result(user, res, work, s)
            if done["queued"]:
                summary["queued"] += 1
                summary["queued_ids"].append(done["evidence_id"])
        summary["results"].append(res)
        if log:
            log(res)
    return summary
