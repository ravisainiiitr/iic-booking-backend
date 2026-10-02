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
    r"link\s+(to\s+|with\s+)?(a\s+|my\s+)?(faculty|supervisor|guide)|my\s+(supervisor|guide)|faculty\s+wallet)\b"
)
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
    r"what\s+can\s+i\s+(ask|do)|show\s+me\s+(the\s+)?options)\b"
)
_CHARGES_GENERIC_RE = re.compile(r"^(what\s+are\s+)?(the\s+)?(analysis\s+)?(charges|rates|prices|pricing|tariffs?|fees)(\s+list)?$")


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

    if _HELP_RE.search(norm) and len(norm.split()) <= 6:
        return Detected("help")

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
    if _RESULTS_RE.search(norm) and not re.search(r"\bsearch\s+results?\b", norm):
        return Detected("results", target)
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
)
