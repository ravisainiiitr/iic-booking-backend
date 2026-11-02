"""Manufacturer references for an instrument: public pages and PDFs about its make and model, fetched by URL.

Staff who look after an instrument (Main Administrator, its Department Administrator, its Officer in Charge or Lab
Operator) add the URLs of the manufacturer's product page, datasheet, manual or application notes. The portal fetches
each one, keeps its text as a ``KnowledgeDocument`` of category ``manufacturer_reference`` scoped to the instrument,
and indexes it for the IITR Assistant's answers, which cite the URL. The instrument's own record is never changed.

Fetching rules:
- http(s) only, to public addresses (a host that resolves to a private, loopback or link-local address is refused);
- the site's robots.txt is respected for our user agent;
- HTML or PDF only, at most ``MAX_BYTES``;
- the text is stored as untrusted reference material: answers treat it as excerpts, never as instructions.
A URL on the manufacturer's own domain (``OFFICIAL_DOMAINS``) is marked official. References older than
``REFRESH_AFTER`` are fetched again by a monthly task; a failed refresh keeps the last good text.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django.utils import timezone

logger = logging.getLogger(__name__)

USER_AGENT = "IIC-IITR-Assistant/1.0 (+https://equip.iitr.ac.in; equipment documentation)"
MAX_BYTES = 15 * 1024 * 1024
MAX_TEXT = 200_000
TIMEOUT = 20
REFRESH_AFTER = timedelta(days=30)
KINDS = {
    "product_page": "Product page",
    "datasheet": "Datasheet",
    "manual": "User manual",
    "application_note": "Application note",
    "other": "Technical documentation",
}

# Manufacturer word (first distinctive word of the equipment's Make, lower case) -> its own web domains.
OFFICIAL_DOMAINS: dict[str, tuple[str, ...]] = {
    "agilent": ("agilent.com",), "anton": ("anton-paar.com",), "beckman": ("beckman.com",), "bio": ("bio-rad.com",),
    "bruker": ("bruker.com",), "carl": ("zeiss.com",), "zeiss": ("zeiss.com",), "elementar": ("elementar.com",),
    "evident": ("evidentscientific.com", "olympus-lifescience.com"), "fei": ("thermofisher.com", "fei.com"),
    "hitachi": ("hitachi-hightech.com",), "horiba": ("horiba.com",), "jasco": ("jascoinc.com", "jasco-global.com"),
    "jeol": ("jeol.com", "jeol.co.jp"), "keysight": ("keysight.com",), "leica": ("leica-microsystems.com",),
    "malvern": ("malvernpanalytical.com",), "panalytical": ("malvernpanalytical.com",), "mettler": ("mt.com",),
    "metrohm": ("metrohm.com",), "micromeritics": ("micromeritics.com",), "netzsch": ("netzsch.com",),
    "nikon": ("nikon.com",), "olympus": ("olympus-lifescience.com", "evidentscientific.com"),
    "oxford": ("oxinst.com",), "park": ("parksystems.com",), "perkinelmer": ("perkinelmer.com", "revvity.com"),
    "quantachrome": ("anton-paar.com",), "renishaw": ("renishaw.com",), "rigaku": ("rigaku.com",),
    "sciex": ("sciex.com",), "shimadzu": ("shimadzu.com", "shimadzu.co.in"), "tescan": ("tescan.com",),
    "thermo": ("thermofisher.com",), "ta": ("tainstruments.com",), "varian": ("agilent.com",),
    "waters": ("waters.com",), "witec": ("witec.de",), "xenocs": ("xenocs.com",),
}
_MAKE_SKIP = {"m/s", "ms", "the", "company", "co"}


class ReferenceError_(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Fetched:
    url: str
    title: str
    text: str
    source_type: str  # html | pdf
    pages: list[str] | None = None


def maker_word(make: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (make or "").lower()) if w not in _MAKE_SKIP]
    return words[0] if words else ""


def is_official(make: str, url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    domains = OFFICIAL_DOMAINS.get(maker_word(make), ())
    return any(host == d or host.endswith("." + d) for d in domains)


def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReferenceError_("bad_url", "Use a full http(s) web address.")
    if parts.username or parts.password:
        raise ReferenceError_("bad_url", "Addresses with a user name or password are not accepted.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise ReferenceError_("unreachable", "That website could not be found.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ReferenceError_("bad_url", "Only public websites can be used as references.")


def robots_allow(url: str, *, opener=None) -> bool:
    parts = urlsplit(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots, headers={"User-Agent": USER_AGENT})
        with (opener or _open)(req, timeout=TIMEOUT) as resp:
            rp.parse(resp.read(512 * 1024).decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as exc:
        return exc.code == 404  # no robots.txt: allowed; 401/403/5xx: treat as not allowed
    except Exception:  # noqa: BLE001 - robots.txt unreachable: be cautious
        return False
    return rp.can_fetch(USER_AGENT, url)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another public http(s) address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req, timeout):
    return urllib.request.build_opener(_SafeRedirect).open(req, timeout=timeout)


class _Text(HTMLParser):
    """Visible text of an HTML page: no scripts, styles, navigation or forms; block elements become line breaks."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe", "template"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    text = "\n".join(line for line in lines if len(line) > 2)
    return " ".join(p.title.split())[:300], text[:MAX_TEXT]


def fetch(url: str, *, opener=None) -> Fetched:
    """Fetch one reference (public address, robots.txt respected, HTML or PDF, size-limited)."""
    from iic_booking.research_copilot.services import pdf_extract

    _check_public(url)
    if not robots_allow(url, opener=opener):
        raise ReferenceError_("robots", "The website does not allow automated reading of that page (robots.txt).")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf"})
    try:
        with (opener or _open)(req, timeout=TIMEOUT) as resp:
            final = resp.geturl() if hasattr(resp, "geturl") else url
            if final != url:
                _check_public(final)  # a redirect must not lead to a private address
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = resp.read(MAX_BYTES + 1)
    except ReferenceError_:
        raise
    except urllib.error.HTTPError as exc:
        raise ReferenceError_("http", f"The website answered {exc.code}.")
    except Exception:  # noqa: BLE001
        raise ReferenceError_("unreachable", "The page could not be fetched.")
    if len(data) > MAX_BYTES:
        raise ReferenceError_("too_large", "The page or file is larger than 15 MB.")
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        try:
            pdf = pdf_extract.extract_pages(data)
        except pdf_extract.PdfRejected as exc:
            raise ReferenceError_("pdf", exc.message)
        name = urlsplit(final).path.rsplit("/", 1)[-1] or "Document"
        return Fetched(url=final, title=name[:300], text="\n".join(pdf.pages)[:MAX_TEXT], source_type="pdf",
                       pages=pdf.pages)
    if ctype not in ("text/html", "application/xhtml+xml", "text/plain", ""):
        raise ReferenceError_("type", "Only web pages (HTML) and PDF files can be used.")
    title, text = html_text(data.decode("utf-8", "replace"))
    if len(text) < 200:
        raise ReferenceError_("empty", "The page has too little readable text (it may need a browser to load).")
    return Fetched(url=final, title=title, text=text, source_type="html")


def _store(doc, eq, fetched: Fetched, kind: str) -> bool:
    from iic_booking.research_copilot.services.ingestion import PAGE_BREAK, content_hash

    label = KINDS.get(kind, KINDS["other"])
    who = " ".join(x for x in (eq.make, eq.model_information) if x) or eq.name
    doc.title = f"{who} — {label}: {fetched.title or urlsplit(fetched.url).netloc}"[:512]
    text = PAGE_BREAK.join(fetched.pages) if fetched.pages else fetched.text
    new_hash = content_hash(text)
    changed = new_hash != doc.content_hash
    doc.content_text, doc.content_hash = text, new_hash
    if changed:
        from iic_booking.research_copilot.models import IndexStatus

        doc.index_status = IndexStatus.STALE
    doc.source_type = fetched.source_type
    doc.external_url = fetched.url[:200] if len(fetched.url) <= 200 else doc.external_url
    doc.source_uri = fetched.url[:1024]
    doc.retrieved_at = timezone.now()
    doc.is_official = is_official(eq.make, fetched.url)
    doc.error_message = ""
    doc.tags = ["manufacturer_reference", kind, "official" if doc.is_official else "other_source"]
    doc.save()
    return changed


def add(equipment, url: str, kind: str, *, user=None, opener=None, index_now: bool = True):
    """Fetch and index a new reference for the instrument. Raises ReferenceError_ with a user-facing message."""
    from iic_booking.research_copilot.models import DocumentCategory, DocumentStatus, KnowledgeDocument, SecurityLevel
    from iic_booking.research_copilot.services.ingestion import index_document

    url = (url or "").strip()
    if kind not in KINDS:
        kind = "other"
    if KnowledgeDocument.objects.filter(equipment_id=equipment.pk, category=DocumentCategory.MANUFACTURER,
                                        source_uri=url[:1024]).exclude(status=DocumentStatus.ARCHIVED).exists():
        raise ReferenceError_("duplicate", "This address is already a reference for the instrument.")
    fetched = fetch(url, opener=opener)
    doc = KnowledgeDocument(
        category=DocumentCategory.MANUFACTURER, security_level=SecurityLevel.PUBLIC, equipment_id=equipment.pk,
        status=DocumentStatus.ACTIVE, created_by=user if getattr(user, "pk", None) else None, version="1.0",
    )
    _store(doc, equipment, fetched, kind)
    if index_now:
        index_document(doc)
    return doc


def refresh(doc, *, opener=None, index_now: bool = True) -> bool:
    """Fetch again; re-index only when the text changed. A failure keeps the last good text and records the error."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.research_copilot.services.ingestion import index_document

    eq = Equipment.objects.filter(pk=doc.equipment_id).first()
    if eq is None:
        return False
    kind = next((t for t in (doc.tags or []) if t in KINDS), "other")
    try:
        fetched = fetch(doc.source_uri or doc.external_url, opener=opener)
    except ReferenceError_ as exc:
        doc.error_message = exc.message[:500]
        doc.save(update_fields=["error_message", "updated_at"])
        return False
    if _store(doc, eq, fetched, kind) and index_now:
        index_document(doc)
    return True


def refresh_stale(now=None) -> dict:
    from iic_booking.research_copilot.models import DocumentCategory, DocumentStatus, KnowledgeDocument
    from django.db.models import Q

    now = now or timezone.now()
    due = KnowledgeDocument.objects.filter(category=DocumentCategory.MANUFACTURER, status=DocumentStatus.ACTIVE).filter(
        Q(retrieved_at__isnull=True) | Q(retrieved_at__lt=now - REFRESH_AFTER))
    ok = failed = 0
    for doc in due[:200]:
        if refresh(doc):
            ok += 1
        else:
            failed += 1
    return {"refreshed": ok, "failed": failed}
