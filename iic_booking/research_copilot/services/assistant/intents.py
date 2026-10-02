"""
Day-to-day intents for the Booking Assistant (pure: no database, no network).

`detect(text)` normalises the message (typos, simple Hinglish), then walks a priority-ordered rule
table and returns the first intent that matches, with the parameters the handler needs (booking
reference, ordinal, list filter, ...). Anything it does not recognise returns None so the existing
availability / equipment / RAG layers answer as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- normalisation

_TYPOS = {
    "bokings": "bookings", "bookigs": "bookings", "bookins": "bookings", "bookngs": "bookings", "booikngs": "bookings",
    "boookings": "bookings", "bookinga": "bookings", "bookign": "booking", "boking": "booking", "bookng": "booking",
    "bookimg": "booking", "booing": "booking", "bking": "booking", "bkng": "booking", "bookin": "booking",
    "walet": "wallet", "wallat": "wallet", "wallte": "wallet", "wollet": "wallet", "wlt": "wallet",
    "balence": "balance", "balanace": "balance", "blance": "balance", "balnce": "balance", "bal": "balance",
    "recharg": "recharge", "rechage": "recharge", "richarge": "recharge", "recahrge": "recharge", "rechareg": "recharge",
    "recharj": "recharge", "rechrge": "recharge", "topup": "top up",
    "cancle": "cancel", "cancell": "cancel", "cansel": "cancel", "cancal": "cancel", "cancl": "cancel",
    "resechdule": "reschedule", "reschedul": "reschedule", "rescedule": "reschedule", "reshedule": "reschedule",
    "reschdule": "reschedule", "rescheudle": "reschedule", "reskedule": "reschedule",
    "resutls": "results", "reslts": "results", "rsults": "results", "reult": "result", "resuts": "results",
    "invoce": "invoice", "invocie": "invoice", "invioce": "invoice",
    "transcations": "transactions", "transations": "transactions", "trasactions": "transactions",
    "tranactions": "transactions", "transction": "transaction", "txns": "transactions", "txn": "transaction",
    "waitlst": "waitlist", "watlist": "waitlist", "waitinglist": "waiting list",
    "tikcet": "ticket", "tiket": "ticket", "tickt": "ticket",
    "templete": "template", "tempalte": "template", "templat": "template",
    "upcomming": "upcoming", "upcomng": "upcoming", "upcming": "upcoming",
    "recnt": "recent", "resent": "recent", "pevious": "previous", "previos": "previous",
    "chrges": "charges", "charegs": "charges", "chages": "charges",
    "paramters": "parameters", "parametres": "parameters", "parms": "parameters", "params": "parameters",
    "detials": "details", "deatils": "details",
    "studnets": "students", "studnet": "student",
    "plz": "please", "pls": "please", "u": "you", "ur": "your", "r": "are", "wat": "what", "wht": "what",
}
_HINGLISH = {
    "mera": "my", "meri": "my", "mere": "my", "apna": "my", "apni": "my",
    "dikhao": "show", "dikha": "show", "dikhado": "show", "dikhaiye": "show", "batao": "tell", "bata": "tell",
    "bataiye": "tell", "kitna": "how much", "kitne": "how much", "kitni": "how much",
    "kaise": "how", "kese": "how", "kaisey": "how", "kaesa": "how", "kya": "what", "kab": "when",
    "paisa": "money", "paise": "money",
    "radd": "cancel", "raddh": "cancel", "rad": "cancel", "badlo": "change", "badalna": "change", "badalni": "change",
    "karna": "do", "karu": "do", "karoon": "do", "karein": "do", "karo": "do", "kare": "do", "karen": "do", "kar": "do",
    "karni": "do", "karte": "do", "hai": "is", "hain": "are", "chahiye": "need", "chahta": "want", "chahti": "want",
    "aaj": "today", "agla": "next", "agli": "next", "pichla": "previous", "pichli": "previous", "sabhi": "all", "sab": "all",
}
_DROP = {"ka", "ki", "ke", "ko", "se", "wala", "wali", "wale", "ji", "na", "toh"}
_CLEAN_RE = re.compile(r"[^a-z0-9#/\-' ]+")


def normalize(text: str) -> str:
    lower = _CLEAN_RE.sub(" ", (text or "").lower().replace("’", "'"))
    words: list[str] = []
    for w in lower.split():
        w = w.strip("'-")
        if not w or w in _DROP:
            continue
        w = _TYPOS.get(w, w)
        w = _HINGLISH.get(w, w)
        words.append(w)
    return " ".join(" ".join(words).split())


# --------------------------------------------------------------------------- params

_ORDINAL_WORDS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "last": -1,
}
_ORDINAL_RE = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|sixth|6th|seventh|7th|eighth|8th|last)"
    r"(?:\s+(?:one|booking|item|entry))?\b|\b(?:number|no|option|item|#)\s*(\d{1,2})\b"
)
_THAT_ONE_RE = re.compile(r"\b(that|this|the same|same)\s+(one|booking)\b|\b(cancel|reschedule|edit|open|show)\s+(it|that|this)\b")
_NUMERIC_REF_RE = re.compile(r"(?:\bbooking\s*(?:id|no|number|ref)?\s*[#:]?\s*|#\s*)(\d{1,9})\b", re.IGNORECASE)

STATUS_WORDS = {
    "pending": ("PENDING",),
    "booked": ("BOOKED",),
    "confirmed": ("BOOKED",),
    "completed": ("COMPLETED",),
    "finished": ("COMPLETED",),
    "done": ("COMPLETED",),
    "cancelled": ("CANCELLED",),
    "canceled": ("CANCELLED",),
    "refunded": ("REFUNDED",),
    "processing": ("PROCESSING",),
    "waitlisted": ("WAITLISTED",),
    "hold": ("HOLD",),
    "unpaid": ("PENDING_PAYMENT",),
    "disrupted": ("DISRUPTION_PENDING", "UNDER_MAINTENANCE", "OTHER_DISRUPTION"),
}


def booking_ref(text: str) -> str | None:
    from iic_booking.research_copilot.services.booking_refs import VIRTUAL_REF_RE

    m = VIRTUAL_REF_RE.search(text or "")
    if m:
        return m.group(1).upper()
    n = _NUMERIC_REF_RE.search(text or "")
    return n.group(1) if n else None


def ordinal(norm: str) -> int | None:
    m = _ORDINAL_RE.search(norm)
    if not m:
        return None
    if m.group(1):
        return _ORDINAL_WORDS[m.group(1)]
    return int(m.group(2))


def refers_to_last(norm: str) -> bool:
    return bool(_THAT_ONE_RE.search(norm))


# --------------------------------------------------------------------------- rules

@dataclass
class Detected:
    intent: str
    params: dict[str, Any] = field(default_factory=dict)


_HOWTO_RE = re.compile(
    r"^(how|steps?|guide|explain|procedure|process)\b|\bhow\s+(do|does|can|to|should|will|is|much time)\b|"
    r"\b(what\s+is\s+the\s+(process|procedure|way)|is\s+it\s+possible|can\s+i|could\s+i|am\s+i\s+able)\b|"
    r"\b(steps|procedure|process)\s+(to|for)\b|\bhow$"
)
_POLICY_RE = re.compile(r"\b(polic\w*|rules?|window|deadline|cut-?off|allowed|charges?\s+for\s+cancel\w*|penalt\w*|fine)\b")
_BOOKING_NOUN = r"(bookings?|reservations?|sessions?|slots?|appointments?|requests?)"
_CANCEL_RE = re.compile(r"\b(cancel(?!l?ed\b)\w*|call\s+off|withdraw|delete\s+(my\s+)?booking)\b")
_RESCHEDULE_RE = re.compile(
    r"\b(reschedul\w*|postpone|prepone|re-?book\s+for\s+another|change\s+(the\s+|my\s+)?(date|time|slot|day)|"
    r"(move|shift)\s+(it|my|the|this|that|booking|slot)\b)"
)
_EDIT_RE = re.compile(
    r"\b(edit|change|modify|update|correct|fix|alter)\b.{0,40}\b(param\w*|inputs?|details|sample\s*(count|set|details|info\w*)?s?|"
    r"number\s+of\s+samples|form|fields?|values?)\b|\bedit\s+(my\s+)?booking\b"
)
_MESSAGE_RE = re.compile(
    r"\b(message|msg|contact|talk\s+to|chat\s+with|write\s+to|email|mail|reach|ask|tell|inform|query\s+to)\b.{0,30}"
    r"\b(lab|labs|operator|oic|in-?charge|staff|officer)\b"
)
_RESULTS_RE = re.compile(
    r"\b(results?|data\s+files?|output\s+files?|report\s+files?|analysis\s+(data|report)|download\s+(my\s+)?(data|files?))\b"
)
_INVOICE_RE = re.compile(r"\b(invoices?|proforma|pro\s*-?\s*forma|bills?|billing|receipts?|tax\s+invoice)\b")
_TX_RE = re.compile(
    r"\b(transactions?|statement|passbook|ledger|debits?|deductions?|spending\s+history|wallet\s+history|"
    r"history\s+of\s+(my\s+)?wallet|where\s+did\s+my\s+money\s+go|money\s+deducted)\b"
)
_RECHARGE_RE = re.compile(
    r"\b(recharge\w*|top\s*up|add\s+(money|funds?|balance|amount|credit)|deposit|load\s+(money|wallet)|"
    r"fund\s+(my\s+)?wallet|bank\s+transfer|pay\s+online|online\s+payment|cash\s+deposit)\b"
)
_BALANCE_RE = re.compile(
    r"\b(balance|how\s+much\s+(money|funds?|is\s+(left|there|in))|funds?\s+(left|available|remaining)|"
    r"money\s+(left|in\s+(my\s+)?wallet)|my\s+wallet|wallet)\b"
)
_WAITLIST_RE = re.compile(r"\b(wait\s*-?\s*list\w*|waiting\s+list|queue)\b")
_URGENT_RE = re.compile(r"\b(urgent|emergency|priority\s+(booking|slot)|rush|asap\s+booking)\b")
_TEMPLATE_RE = re.compile(r"\b(templates?|saved\s+(inputs|bookings?|settings)|reuse\s+(my\s+)?(inputs|booking|settings))\b")
_TICKET_RE = re.compile(r"\b(tickets?|helpdesk|help\s+desk|grievance|complain\w*|support\s+(request|team)|raise\s+(an?\s+)?issue)\b")
_TICKET_CREATE_RE = re.compile(r"\b(raise|create|open|new|file|log|submit|lodge|register|want)\b")
_RATE_RE = re.compile(
    r"\brat(e|ing)\s+(my|the|your|this|a|our)?\s*(experience|booking|service|lab|session|operator)\b|"
    r"\b(give|submit|leave|share|provide)\s+(a\s+|my\s+)?(rating|feedback|review|stars?)\b|\bratings?\b|"
    r"\bfeedback\s+(for|on|about)\s+(my\s+)?(booking|session|lab|service)\b"
)
_MY_RESEARCH_RE = re.compile(r"\b(my\s+research|workspaces?|research\s+groups?|shared\s+data|lab\s+notebook)\b")
_STUDENTS_RE = re.compile(
    r"\b(my\s+students?|students?\s+(list|management|limits?|spending|bookings?)|manage\s+(my\s+)?students?|"
    r"spending\s+limits?|student\s+limits?|link\s+(a\s+|my\s+)?students?|add\s+(a\s+)?students?|"
    r"remove\s+(a\s+)?students?|students?\s+linked|associate\s+(with\s+)?(a\s+|my\s+)?(faculty|supervisor|guide)|"
    r"link\s+(to\s+|with\s+)?(a\s+|my\s+)?(faculty|supervisor|guide)|my\s+(supervisor|guide)|faculty\s+wallet|"
    r"students?\s+(join|link|connect)\w*|join\w*\s+(a\s+|my\s+|the\s+|your\s+)?((faculty|supervisor|guide)'?s?\s+)?wallet|"
    r"link\w*\s+(a\s+|my\s+|the\s+)?wallet|wallet\s+link\w*|(supervisor|faculty|guide)'?s?\s+(name|wallet)|join\s+requests?)\b"
)
_WHO_PAYS_RE = re.compile(
    r"\b(who\s+pays|(charged|billed|pay|priced)\s+as|pi\s+(pricing|rates?|prices?|charges?)|"
    r"(pricing|rates?|prices?|charges?)\s+for\s+(a\s+|the\s+)?pi|wallet\s+owner|"
    r"which\s+wallet\s+(is|gets|will\s+be)\s+(charged|debited))\b"
)
_COST_RE = re.compile(r"\b(how\s+much|costs?|costing|price[sd]?|pricing|charges?|fees?|rates?|tariffs?|expensive)\b")
_SAMPLES_RE = re.compile(r"\b(\d{1,3})\s+(?:[a-z0-9'-]+\s+){0,2}(samples?|specimens?|runs?|measurements?)\b")
_DURATION_RE = re.compile(r"\b\d+(\.\d+)?\s*(hours?|hrs?|h|minutes?|mins?)\b")
_PREPARE_RE = re.compile(
    r"\bprepar\w*\b|\bwhat\s+(should|do|must)\s+i\s+(bring|carry|need\s+to\s+bring)\b|"
    r"\bbefore\s+(coming|i\s+come|my\s+(\w+\s+)?(booking|slot|session|appointment|measurement))\b"
)
_SUBMISSION_RE = re.compile(
    r"\bsubmi\w*\b.{0,30}\b(by\s+when|when|deadline|last\s+date|till|until|by\s+what\s+time)\b|"
    r"\b(when|deadline|last\s+date)\b.{0,30}\bsubmi\w*\b|\bsample\s+submission\b|\bhow\s+(do|can|should)\s+i\s+submit\b"
)
_LAB_HOURS_RE = re.compile(
    r"\b(lab|labs|laboratory|facility|iic|centre|center|office)\s+(access\s+)?(hours?|timings?|working\s+hours|opening\s+hours)\b|"
    r"\b(working|office|opening|access)\s+hours\b|\blab\s+access\b|"
    r"\bwhen\s+(is|does|do)\s+(the\s+)?(lab|labs|iic|facility|centre|center)\s+(open|close)\w*\b"
)
_ANALYSIS_SOFTWARE_RE = re.compile(
    r"\b(software|softwares|imagej|fiji|digital\s*micrograph|gatan|highscore|x'?pert|jade|fullprof|gsas|vesta|"
    r"dm3|dm4|workstations?)\b"
)
_ANALYSIS_REMOTE_RE = re.compile(
    r"\bremote(ly)?\b.{0,30}\banaly\w*|\banaly\w*\b.{0,30}\bremote(ly)?\b|"
    r"\banalysis\s+(pc|computer|workstation|machine|session|workspace|environment)\b"
)
_ANALYSIS_FILES_RE = re.compile(
    r"\b(files?|data)\b.{0,40}\b(available|after|ready|download\w*|stored|kept|saved|happen\w*|transfer\w*|copy|copied)\b|"
    r"\b(where|which|what)\b.{0,30}\b(files?|data)\b|\banaly[sz]ed\s+(data|files?|results?)\b|"
    r"\bdownload\w*\b.{0,20}\b(data|files?)\b|\braw\s+data\b"
)
_FIND_EQUIPMENT_RE = re.compile(
    r"^(please\s+)?(help\s+me\s+|can\s+you\s+help\s+me\s+|i\s+want\s+to\s+|i\s+need\s+to\s+)?"
    r"(find|choose|pick|select|suggest|recommend)\s+(a\s+|the\s+)?(suitable|right|best|correct)?\s*"
    r"(equipment|instruments?|technique|machine)(\s+for\s+(my|a|the)\s+(sample|samples|research|work|project))?$"
)
_SMALLTALK_RE = re.compile(
    r"^(ok|okay|okk+|k|kk|thanks|thank\s+you|thanks\s+a\s+lot|thank\s+you\s+so\s+much|thx|ty|got\s+it|noted|cool|great|"
    r"nice|fine|alright|all\s+right|sure|good|perfect|done|understood)"
    r"(\s+(thanks|thank\s+you|thx|so\s+much|a\s+lot))?$"
)
_VAGUE_RE = re.compile(
    r"^(can\s+i|could\s+i|may\s+i|should\s+i|what|how|why|when|where|which|who|and|so|then|huh|is\s+it|"
    r"are\s+you\s+sure|really|tell\s+me|more|explain|what\s+else|and\s+then|i\s+have\s+a\s+question|question|query|doubt)$"
)

# Technique / term glossary keys (texts live in `answers`); the regexes run on normalised text.
GLOSSARY_TERMS: tuple[tuple[str, re.Pattern], ...] = (
    ("fwhm", re.compile(r"\bfwhm\b|\bfull\s+width\s+(at\s+)?half\s+max\w*")),
    ("bragg", re.compile(r"\bbragg'?s?\b")),
    ("scherrer", re.compile(r"\bscherrer'?s?\b|\bcrystallite\s+size\b")),
    ("gixrd", re.compile(r"\bgi-?xrd\b|\bgrazing\s+incidence\b|\bthin[\s-]?films?\b")),
    ("pxrd", re.compile(r"\bpxrd\b|\bpowder\s+(xrd|x-?ray\s+diffraction)\b")),
    ("xrd", re.compile(r"\bxrd\b|\bx-?ray\s+diffraction\b")),
    ("fesem", re.compile(r"\bfe-?sem\b|\bfield\s+emission\s+(scanning\s+)?electron\b")),
    ("sem", re.compile(r"\bsem\b|\bscanning\s+electron\b")),
    ("tem", re.compile(r"\b(hr-?)?tem\b|\btransmission\s+electron\b")),
    ("eds", re.compile(r"\b(eds|edx|edax|edxs)\b|\benergy[\s-]dispersive\b")),
    ("xps", re.compile(r"\bxps\b|\bphotoelectron\s+spectro\w*")),
    ("ftir", re.compile(r"\bft-?ir\b|\binfrared\s+spectro\w*")),
    ("raman", re.compile(r"\braman\b")),
    ("bet", re.compile(r"\bbet\b|\bsurface\s+area\s+analy\w*")),
    ("tga", re.compile(r"\btga\b|\bthermogravimetr\w*")),
    ("dsc", re.compile(r"\bdsc\b|\bdifferential\s+scanning\b")),
    ("afm", re.compile(r"\bafm\b|\batomic\s+force\b")),
    ("nmr", re.compile(r"\bnmr\b|\bnuclear\s+magnetic\b")),
    ("icp", re.compile(r"\bicp(-?(ms|oes|aes))?\b|\binductively\s+coupled\b")),
    ("uvvis", re.compile(r"\buv-?vis\w*\b|\buv\s+vis\w*\b")),
)
_GLOSSARY_Q_RE = re.compile(
    r"^(what\s+(is|are|does|do)|what's|whats|define|definition\s+of|meaning\s+of|explain|tell\s+me\s+about|"
    r"full\s+form\s+of|difference\s+between|how\s+does)\b|\b(vs|versus|difference\s+between|stands?\s+for|full\s+form|"
    r"briefly|in\s+simple\s+words)\b|^(can|could|does|is)\s+.{0,20}\bthin[\s-]?films?\b"
)
_GLOSSARY_NOT_RE = re.compile(
    r"\b(my|mine|booking|bookings|book|slots?|availab\w*|free|charges?|costs?|price|pricing|rates?|fees?|location|where|"
    r"contacts?|operators?|oic|status|wallet|instructions?|rules|cancel\w*|results?|today|tomorrow)\b"
)


def glossary_terms(norm: str) -> list[str]:
    found = [key for key, rx in GLOSSARY_TERMS if rx.search(norm)]
    if "gixrd" in found and not re.search(r"\bgi-?xrd\b|\bgrazing\b|\bxrd\b|\bx-?ray\b|\bdiffraction\b", norm):
        found.remove("gixrd")
    if "gixrd" in found and "xrd" in found:
        found.remove("xrd")
    if "fesem" in found and "sem" in found and not re.search(r"(^|\s)sem\b", norm):
        found.remove("sem")
    return found[:3]


def sample_count(norm: str) -> int | None:
    m = _SAMPLES_RE.search(norm)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 500 else None
_REPORTS_RE = re.compile(r"\b(reports?|usage\s+report|utili[sz]ation|analytics|statistics|stats)\b")
_STAFF_TODAY_RE = re.compile(
    r"\b(today'?s?|todays)\s+(bookings?|schedule|sessions?|slots?)\b|\bbookings?\s+(for|on)\s+today\b|"
    r"\b(bookings?|schedule)\s+(on|for|of)\s+my\s+(equipment|instruments?|machines?|lab)\b|"
    r"\bmy\s+(equipment|instrument|lab)'?s?\s+(bookings?|schedule)\b|\bwho\s+(is|are)\s+(booked|coming)\s+today\b"
)
_STAFF_APPROVALS_RE = re.compile(
    r"\b(pending|awaiting)\s+(approvals?|requests?|bookings?\s+to\s+approve)\b|\bapprove\s+(bookings?|requests?)\b|"
    r"\b(approval|approvals)\s+(queue|pending|list)\b|\bto\s+approve\b"
)
_STAFF_QUEUE_RE = re.compile(r"\b(waitlist|waiting\s+list|urgent(\s+request)?s?)\s+(queue|requests?\s+(for|on)\s+my)\b|\b(queue)\s+(on|for)\s+my\b")
_LIST_RE = re.compile(
    rf"\b(my|mine|show|list|view|see|display|check|get|give|all|recent|latest|last|upcoming|future|next|past|previous|old|"
    rf"history|pending|completed|cancelled|canceled|confirmed|booked|refunded|processing|waitlisted|current|active|scheduled)\b"
    rf".{{0,40}}\b{_BOOKING_NOUN}\b|\b{_BOOKING_NOUN}\s+(list|history|status)\b|\bwhat\s+(have|did)\s+i\s+book\w*\b|"
    rf"\bmy\s+(next|upcoming)\s+(slot|session|booking|appointment)\b|\bwhen\s+is\s+my\s+(next\s+)?(booking|slot|session)\b"
)
_NEXT_RE = re.compile(r"\b(next|upcoming)\s+(booking|slot|session|appointment)\b|\bwhen\s+is\s+my\s+(next\s+)?(booking|slot|session)\b")
_DETAILS_RE = re.compile(r"\b(details?|status|info\w*|show|open|view|track|where\s+is|what\s+happened|update|progress)\b")
_HELP_RE = re.compile(
    r"^(help|menu|options|start|what\s+can\s+you\s+do|what\s+do\s+you\s+do|how\s+can\s+you\s+help|"
    r"what\s+can\s+i\s+(ask|do)|show\s+me\s+(the\s+)?options|"
    r"(summari[sz]e|list|show|explain|what\s+are)\s+(me\s+)?(the\s+|your\s+)?((booking\s+)?assistant'?s?\s+|copilot'?s?\s+)?"
    r"(capabilit\w*|features)|what\s+(all\s+)?can\s+(the\s+)?(booking\s+)?(assistant|copilot|bot)\s+do)\b"
)
_CHARGES_GENERIC_RE = re.compile(
    r"^(and\s+)?(what\s+(are|is)\s+|what\s+about\s+|how\s+about\s+)?(the\s+)?(analysis\s+)?"
    r"(charges?|rates?|prices?|pricing|tariffs?|fees?|costs?)(\s+list)?$"
)


def _list_filter(norm: str) -> dict[str, Any]:
    if _NEXT_RE.search(norm):
        return {"scope": "upcoming", "limit": 1}
    statuses: list[str] = []
    for word, values in STATUS_WORDS.items():
        if re.search(rf"\b{word}\b", norm):
            statuses.extend(values)
    scope = "recent"
    if re.search(r"\b(upcoming|future|scheduled|coming|next|current|active)\b", norm):
        scope = "upcoming"
    elif re.search(r"\b(past|previous|old|older|earlier|history|completed|finished)\b", norm):
        scope = "past"
    elif re.search(r"\ball\b", norm):
        scope = "all"
    out: dict[str, Any] = {"scope": scope}
    if statuses:
        out["statuses"] = sorted(set(statuses))
        if scope == "upcoming" and not set(statuses) & {"PENDING", "BOOKED", "DISRUPTION_PENDING", "PENDING_PAYMENT"}:
            out["scope"] = "all"
    return out


def detect(text: str) -> Detected | None:
    """Return the day-to-day intent for a typed message, or None to fall through."""
    norm = normalize(text)
    if not norm:
        return None
    howto = bool(_HOWTO_RE.search(norm))
    ref = booking_ref(text)
    ordn = ordinal(norm)
    target: dict[str, Any] = {}
    if ref:
        target["ref"] = ref
    if ordn is not None:
        target["ordinal"] = ordn
    if refers_to_last(norm):
        target["last"] = True

    words = len(norm.split())
    if _FIND_EQUIPMENT_RE.match(norm):
        return Detected("find_equipment")
    if _HELP_RE.search(norm) and words <= 6:
        return Detected("help")
    if _SMALLTALK_RE.match(norm):
        return Detected("smalltalk")
    if _VAGUE_RE.match(norm):
        return Detected("vague")
    terms = glossary_terms(norm)
    if terms and words <= 14 and _GLOSSARY_Q_RE.search(norm) and not _GLOSSARY_NOT_RE.search(norm):
        return Detected("glossary", {"terms": terms})
    if _LAB_HOURS_RE.search(norm) and not ref:
        return Detected("lab_hours")

    # Booking changes: actions on a booking, or the how-to/policy answer when phrased as a question.
    if _CANCEL_RE.search(norm) and not re.search(r"\bcancel\w*\s+(polic\w*|rules?|charges?|fees?)\b", norm):
        if howto and not target:
            return Detected("howto_cancel")
        if _POLICY_RE.search(norm) and not target:
            return None
        if _WAITLIST_RE.search(norm):
            return Detected("waitlist", {"leave": True})
        return Detected("cancel", {**target, "next": bool(_NEXT_RE.search(norm))})
    if _RESCHEDULE_RE.search(norm):
        if howto and not target:
            return Detected("howto_reschedule")
        if _POLICY_RE.search(norm) and not target:
            return None
        return Detected("reschedule", {**target, "next": bool(_NEXT_RE.search(norm))})
    if _EDIT_RE.search(norm) and not re.search(r"\b(profile|password|email|phone|name)\b", norm):
        if howto and not target:
            return Detected("howto_edit")
        return Detected("edit", {**target, "next": bool(_NEXT_RE.search(norm))})
    if _MESSAGE_RE.search(norm) and not re.search(r"\bwho\s+is\b", norm):
        return Detected("message_lab", target)

    # Staff queues (the handler checks the role).
    if _STAFF_TODAY_RE.search(norm):
        return Detected("staff_today")
    if _STAFF_APPROVALS_RE.search(norm):
        return Detected("staff_approvals")
    if _STAFF_QUEUE_RE.search(norm):
        return Detected("staff_urgent" if "urgent" in norm else "staff_waitlist")

    # Money.
    if _WHO_PAYS_RE.search(norm):
        return Detected("who_pays", {"samples": sample_count(norm)})
    if _RECHARGE_RE.search(norm) and not _INVOICE_RE.search(norm):
        return Detected("recharge")
    if _TX_RE.search(norm):
        return Detected("transactions")
    if _INVOICE_RE.search(norm):
        return Detected("invoices", target)

    # Booking-adjacent features.
    if _WAITLIST_RE.search(norm):
        return Detected("waitlist", {"join": bool(re.search(r"\b(join|add|put|enrol\w*|register|get\s+on)\b", norm)) or howto})
    if _URGENT_RE.search(norm):
        return Detected("urgent", {"howto": howto or bool(re.search(r"\b(request|raise|make|submit|need|want)\b", norm))})
    if _TEMPLATE_RE.search(norm):
        return Detected("templates", {"howto": howto})
    if _RATE_RE.search(norm) and not re.search(r"\b(charges?|price|cost|fees?|tariff)\b", norm):
        return Detected("rate", target)
    if _PREPARE_RE.search(norm) and not ref:
        return Detected("prepare", {"with_cost": bool(_COST_RE.search(norm))})
    if _SUBMISSION_RE.search(norm) and not ref:
        return Detected("sample_submission")
    if _RESULTS_RE.search(norm) and not re.search(r"\bsearch\s+results?\b", norm):
        return Detected("results", target)
    if not re.search(r"\b(status|shared\s+data)\b", norm):
        if _ANALYSIS_SOFTWARE_RE.search(norm):
            return Detected("analysis_help", {"topic": "software"})
        if _ANALYSIS_REMOTE_RE.search(norm):
            return Detected("analysis_help", {"topic": "remote"})
        if _ANALYSIS_FILES_RE.search(norm) and not _BALANCE_RE.search(norm):
            return Detected("analysis_help", {"topic": "files"})
    if _TICKET_RE.search(norm) or re.search(r"\b(talk\s+to\s+(a\s+)?human|contact\s+support|customer\s+care)\b", norm):
        create = bool(_TICKET_CREATE_RE.search(norm)) and not re.search(r"\b(status|my\s+tickets|open\s+tickets|list)\b", norm)
        return Detected("ticket_create" if create else "tickets")
    if _STUDENTS_RE.search(norm):
        return Detected("students", {"howto": howto})
    if _MY_RESEARCH_RE.search(norm):
        return Detected("my_research", {"howto": howto})
    if _REPORTS_RE.search(norm) and not _RESULTS_RE.search(norm):
        return Detected("reports")

    # Bookings: details for a reference, otherwise a list.
    if ref and (not howto or _DETAILS_RE.search(norm)):
        return Detected("booking_details", {"ref": ref})
    if ordn is not None and _DETAILS_RE.search(norm) and len(norm.split()) <= 6:
        return Detected("booking_details", {"ordinal": ordn})
    if ordn is not None and len(norm.split()) <= 4:
        return Detected("booking_details", {"ordinal": ordn, "soft": True})
    samples = sample_count(norm)
    if (
        _COST_RE.search(norm)
        and (samples or re.search(r"\b(how\s+much|costs?|costing|expensive)\b", norm))
        and not _DURATION_RE.search(norm)
        and not _BALANCE_RE.search(norm)
        and not _CHARGES_GENERIC_RE.search(norm)
    ):
        return Detected("estimate", {"samples": samples})
    if _LIST_RE.search(norm) and not howto:
        return Detected("bookings", _list_filter(norm))

    if _BALANCE_RE.search(norm) and (not howto or re.search(r"\bhow\s+much\b|\bbalance\b", norm)):
        return Detected("balance")
    if _BALANCE_RE.search(norm) and re.search(r"\bwallet\b", norm):
        return Detected("howto_wallet")
    if _CHARGES_GENERIC_RE.search(norm):
        return Detected("charges_generic")
    return None


INTENTS = (
    "help", "howto_cancel", "howto_reschedule", "howto_edit", "howto_wallet", "cancel", "reschedule", "edit",
    "message_lab", "staff_today", "staff_approvals", "staff_urgent", "staff_waitlist", "recharge", "transactions",
    "invoices", "waitlist", "urgent", "templates", "rate", "results", "ticket_create", "tickets", "students",
    "my_research", "reports", "booking_details", "bookings", "balance", "charges_generic",
    "smalltalk", "vague", "find_equipment", "glossary", "lab_hours", "who_pays", "prepare", "sample_submission",
    "analysis_help", "estimate",
)
