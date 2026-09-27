"""
Scored intent classification.

Deterministic weighted rules (no model call), so the same message always maps to the same intent
and the result can be unit tested. Confidence is internal only (HIGH / MEDIUM / LOW) and is never
shown to users as a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from iic_booking.research_copilot.services.intelligence import terminology
from iic_booking.research_copilot.services.intelligence.entities import Entities

GREETING = "GREETING"
BARE_TERM = "BARE_TERM"
EQUIPMENT_SEARCH = "EQUIPMENT_SEARCH"
EQUIPMENT_INFORMATION = "EQUIPMENT_INFORMATION"
EQUIPMENT_COMPARISON = "EQUIPMENT_COMPARISON"
EQUIPMENT_RECOMMENDATION = "EQUIPMENT_RECOMMENDATION"
AVAILABILITY = "AVAILABILITY"
BOOKING_REQUEST = "BOOKING_REQUEST"
CANCELLATION_REQUEST = "CANCELLATION_REQUEST"
PARTIAL_CANCELLATION = "PARTIAL_CANCELLATION"
RESCHEDULING_REQUEST = "RESCHEDULING_REQUEST"
MY_BOOKINGS = "MY_BOOKINGS"
NEXT_BOOKING = "NEXT_BOOKING"
WALLET_BALANCE = "WALLET_BALANCE"
WALLET_TRANSACTIONS = "WALLET_TRANSACTIONS"
WALLET_RECHARGE = "WALLET_RECHARGE"
CREDIT_STATUS = "CREDIT_STATUS"
COST_ESTIMATE = "COST_ESTIMATE"
RESULT_STATUS = "RESULT_STATUS"
SAMPLE_STATUS = "SAMPLE_STATUS"
REMOTE_ANALYSIS = "REMOTE_ANALYSIS"
MY_RESEARCH = "MY_RESEARCH"
RESEARCH_GROUP = "RESEARCH_GROUP"
PORTAL_HELP = "PORTAL_HELP"
SUPPORT_REQUEST = "SUPPORT_REQUEST"
UNKNOWN = "UNKNOWN"

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

# Live personal data already answered by the V2 deterministic read tools.
LIVE_READ_INTENTS = frozenset(
    {
        MY_BOOKINGS,
        NEXT_BOOKING,
        WALLET_BALANCE,
        WALLET_TRANSACTIONS,
        WALLET_RECHARGE,
        CREDIT_STATUS,
        RESULT_STATUS,
        SAMPLE_STATUS,
        REMOTE_ANALYSIS,
    }
)

_R = re.compile

_RULES: list[tuple[str, int, re.Pattern]] = [
    (GREETING, 6, _R(r"^(hi|hello|hey|hii+|good (morning|afternoon|evening)|namaste)[\s!.,]*$")),
    (SUPPORT_REQUEST, 6, _R(r"\b(talk|speak|chat|connect)\s+(to|with)\s+(a\s+)?(human|person|someone|admin|staff|support|oic)\b")),
    (SUPPORT_REQUEST, 6, _R(r"\b(raise|create|open|file|log|submit)\s+(a\s+)?(support\s+)?(ticket|complaint)\b")),
    (SUPPORT_REQUEST, 5, _R(r"\bcontact\s+(support|admin|the admin|helpdesk)\b")),
    (CANCELLATION_REQUEST, 5, _R(r"\bcancel(l?ation|l?ing|l?ed)?\b")),
    (RESCHEDULING_REQUEST, 6, _R(r"\b(re-?schedul\w*|postpone|prepone|shift my booking|move my booking)\b")),
    (RESCHEDULING_REQUEST, 4, _R(r"\bchange\s+(the\s+)?(date|time|slot)\s+of\s+(my\s+)?booking\b")),
    (BOOKING_REQUEST, 4, _R(r"\b(book|reserve)\b(?!ing)")),
    (BOOKING_REQUEST, 3, _R(r"\b(i\s+(want|need|would like)\s+to\s+use|make\s+a\s+booking|new\s+booking)\b")),
    (AVAILABILITY, 4, _R(r"\b(slots?|availability|available\s+(slots?|dates?|times?)|free\s+slots?|open\s+slots?)\b")),
    (AVAILABILITY, 3, _R(r"\b(when\s+can\s+i|is\s+.+\s+free|earliest|first available|next available)\b")),
    (AVAILABILITY, 4, _R(r"\b(available|free|open)\b.*\b(today|tomorrow|tonight|this\s+week|next\s+week|weekend|"
                         r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening|on\s+\d)\b")),
    (AVAILABILITY, 2, _R(r"\b(is|are)\s+(the\s+)?\S+(\s+\S+)?\s+available\b")),
    (COST_ESTIMATE, 4, _R(r"\b(cost|costs|price|pricing|charges?|rates?|fees?|tariff|how\s+much|estimate)\b")),
    (MY_BOOKINGS, 5, _R(r"\bmy\s+(recent\s+|current\s+|all\s+|past\s+)?bookings\b|\blist\s+(my\s+)?bookings\b|\bbooking\s+history\b")),
    (NEXT_BOOKING, 5, _R(r"\b(next|upcoming)\s+booking\b")),
    (WALLET_BALANCE, 4, _R(r"\b(wallet|balance)\b")),
    (WALLET_TRANSACTIONS, 5, _R(r"\b(transactions?|statement|debits?|spent|spend)\b")),
    (WALLET_RECHARGE, 6, _R(r"\b(recharge|top.?up|add\s+money|add\s+funds)\b")),
    (CREDIT_STATUS, 5, _R(r"\b(credit\s+(status|limit|request|facility)|outstanding\s+credit|wallet\s+credit)\b")),
    (RESULT_STATUS, 4, _R(r"\b(results?|reports?|data\s+files?|output\s+files?)\b")),
    (SAMPLE_STATUS, 5, _R(r"\b(sample\s+(status|received|accepted|rejected|tracking)|my\s+samples?)\b")),
    (REMOTE_ANALYSIS, 6, _R(r"\b(remote\s+analysis|analysis\s+workspace|remote\s+desktop|analy[sz]e\s+data)\b")),
    (MY_RESEARCH, 4, _R(r"\b(my\s+research|research\s+workspace|workspaces?|publications?)\b")),
    (RESEARCH_GROUP, 6, _R(r"\b(research\s+groups?|group\s+members?|add\s+(a\s+|my\s+)?students?|lab\s+group)\b")),
    (EQUIPMENT_COMPARISON, 5, _R(r"\b(compare|comparison|difference\s+between|versus|vs\.?)\b")),
    (EQUIPMENT_RECOMMENDATION, 4, _R(r"\b(which|what|suitable|best|recommend\w*|suggest\w*)\b.{0,40}\b(equipment|instruments?|techniques?|machines?|characteri[sz]ation|analysis|method)\b")),
    (EQUIPMENT_RECOMMENDATION, 3, _R(r"\b(i\s+need\s+to|i\s+want\s+to|how\s+(can|do)\s+i)\s+(measure|determine|analy[sz]e|identify|characteri[sz]e|find\s+out)\b")),
    (EQUIPMENT_INFORMATION, 4, _R(r"\b(what\s+is|what's|tell\s+me\s+about|explain|details?\s+(of|about)|information\s+(on|about)|specifications?|specs|capabilit\w*|where\s+is|location\s+of|who\s+is\s+the\s+oic)\b")),
    (EQUIPMENT_SEARCH, 4, _R(r"\b(show|list|find|search|browse|see|view)\b.{0,30}\b(equipment|equipments|instruments?|machines?|facilit\w*)\b")),
    (EQUIPMENT_SEARCH, 3, _R(r"\b(show|list|find|search|browse|view)\b")),
    (PORTAL_HELP, 3, _R(r"\bhow\s+(do|can|to|should|does)\b|\bwhere\s+(do|can|should)\b|\bwhat\s+(does|is\s+the\s+(process|procedure|policy))\b")),
    (PORTAL_HELP, 2, _R(r"\b(unable\s+to|can'?t|cannot|not\s+able\s+to|error|problem|issue|not\s+working|help|process|procedure|policy|rule)\b")),
]


@dataclass
class IntentResult:
    intent: str
    confidence: str
    scores: dict[str, int] = field(default_factory=dict)
    runner_up: str | None = None


def classify(text: str, ents: Entities) -> IntentResult:
    lower = terminology.normalize(text)
    if terminology.bare_technique(lower):
        return IntentResult(BARE_TERM, HIGH, {BARE_TERM: 10})

    scores: dict[str, int] = {}
    for intent, weight, pat in _RULES:
        if pat.search(lower):
            scores[intent] = max(scores.get(intent, 0), weight)

    has_equipment = bool(ents.techniques)
    if has_equipment:
        for intent in (EQUIPMENT_SEARCH, EQUIPMENT_INFORMATION, EQUIPMENT_COMPARISON, AVAILABILITY, BOOKING_REQUEST, COST_ESTIMATE):
            if intent in scores:
                scores[intent] += 1
    else:
        scores.pop(EQUIPMENT_COMPARISON, None)
        if EQUIPMENT_SEARCH in scores and scores[EQUIPMENT_SEARCH] < 4:
            scores.pop(EQUIPMENT_SEARCH)
    if ents.purpose_techniques and not has_equipment:
        scores[EQUIPMENT_RECOMMENDATION] = max(scores.get(EQUIPMENT_RECOMMENDATION, 0), 4)
    if EQUIPMENT_INFORMATION in scores and not has_equipment:
        # "What is the cancellation policy" is portal help, not equipment information.
        scores[PORTAL_HELP] = max(scores.get(PORTAL_HELP, 0), scores.pop(EQUIPMENT_INFORMATION))

    if CANCELLATION_REQUEST in scores:
        for loser in (BOOKING_REQUEST, NEXT_BOOKING, MY_BOOKINGS, AVAILABILITY, COST_ESTIMATE, PORTAL_HELP):
            if loser == PORTAL_HELP and re.search(r"\b(policy|rule|refund\s+rule|how\s+(do|can))\b", lower) and not re.search(r"\bmy\b", lower):
                continue
            scores.pop(loser, None)
        if PORTAL_HELP in scores and scores[PORTAL_HELP] >= scores[CANCELLATION_REQUEST]:
            scores.pop(CANCELLATION_REQUEST)
    if RESCHEDULING_REQUEST in scores:
        for loser in (BOOKING_REQUEST, NEXT_BOOKING, MY_BOOKINGS, AVAILABILITY, CANCELLATION_REQUEST):
            scores.pop(loser, None)
    if BOOKING_REQUEST in scores:
        scores.pop(AVAILABILITY, None)
        scores.pop(EQUIPMENT_SEARCH, None)
    if WALLET_RECHARGE in scores or WALLET_TRANSACTIONS in scores or CREDIT_STATUS in scores:
        scores.pop(WALLET_BALANCE, None)
    if WALLET_TRANSACTIONS in scores and not re.search(r"\b(wallet|transactions?|statement|debits?|spent|spend)\b", lower):
        scores.pop(WALLET_TRANSACTIONS)
    if SAMPLE_STATUS in scores:
        scores.pop(RESULT_STATUS, None)
    if RESEARCH_GROUP in scores:
        scores.pop(MY_RESEARCH, None)
        scores.pop(PORTAL_HELP, None)
    if COST_ESTIMATE in scores and not has_equipment and re.search(r"\b(policy|refund|gst)\b", lower):
        scores[PORTAL_HELP] = max(scores.get(PORTAL_HELP, 0), scores.pop(COST_ESTIMATE))
    if PORTAL_HELP in scores and has_equipment and not any(
        i in scores for i in (BOOKING_REQUEST, CANCELLATION_REQUEST, RESCHEDULING_REQUEST, COST_ESTIMATE, AVAILABILITY)
    ):
        # "How do I prepare a sample for FESEM" belongs to the equipment manual answers.
        scores[EQUIPMENT_INFORMATION] = max(scores.get(EQUIPMENT_INFORMATION, 0), scores.pop(PORTAL_HELP))

    if not scores:
        return IntentResult(UNKNOWN, LOW, {})
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    margin = best_score - (runner[1] if runner else 0)
    if best_score >= 5 or (best_score >= 4 and margin >= 1):
        confidence = HIGH
    elif best_score >= 3:
        confidence = MEDIUM
    else:
        confidence = LOW
    return IntentResult(best, confidence, scores, runner[0] if runner else None)
