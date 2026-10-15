"""
Support ticket triage: suggest a ticket type, priority, who should handle it and a reply draft.

Rules only (no AI model): keyword topics, equipment detected from the text (code, name or a technique that only
one instrument offers), and the equipment's Officer in Charge or Lab operator as handler. The reply draft comes
from an approved Copilot answer when one matches, otherwise from a topic template. Staff accept or dismiss the
suggestion; a draft is never sent automatically. Assigning automatically is a separate switch (off by default).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

AUTO_ASSIGN_MIN_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Topic:
    key: str
    ticket_type: str
    route: str  # oic | operator | admin
    pattern: str
    label: str
    draft: str


TOPICS: tuple[Topic, ...] = (
    Topic(
        "payment", "payment", "admin",
        r"\b(wallet|recharge|top ?up|balance|refund\w*|deduct\w*|debit\w*|charged (twice|extra|wrongly)|payment|paid|"
        r"invoice|proforma|receipt|gst|sric|utili[sz]ation certificate|grant|money)\b",
        "Wallet, payments and invoices",
        "Wallet credits, debits and refunds are listed under Wallet > Transactions, and invoices under Proforma "
        "invoice. We are checking the transaction you mention and will update this ticket.",
    ),
    Topic(
        "account", "account", "admin",
        r"\b(login|log in|sign ?in|password|otp|verification|verify|register\w*|registration|profile|account|"
        r"supervisor|faculty (link|approval)|email change|user type|department change)\b",
        "Account, registration and supervisor link",
        "Account details are on your Profile page; supervisor links and registration approvals are shown there "
        "as well. We are checking your account and will update this ticket.",
    ),
    Topic(
        "results", "laboratory", "oic",
        r"\b(results?|data|report|download\w*|spectr\w*|images?|files?|raw data|analy[sz]ed data|zip)\b",
        "Results and data",
        "Results are published on the booking's details in My Bookings (Raw Data and Analyzed Data). We have asked "
        "the lab about the results for your booking and will update this ticket.",
    ),
    Topic(
        "sample", "laboratory", "operator",
        r"\b(sample\w*|courier|submission|submit\w*|collect\w*|dispos\w*|return(ed)? (my )?sample)\b",
        "Samples: submission, collection and disposal",
        "Sample submission deadlines and the sample status are shown on each booking in My Bookings. We have "
        "passed your question to the lab and will update this ticket.",
    ),
    Topic(
        "equipment_issue", "equipment", "oic",
        r"\b(not working|broken|breakdown|down|maintenance|repair|fault\w*|error on (the )?(instrument|machine)|"
        r"calibrat\w*|vacuum|detector|filament)\b",
        "Instrument problem",
        "Thank you for reporting this. The Officer in Charge has been informed and will update this ticket once "
        "the instrument has been checked.",
    ),
    Topic(
        "booking", "booking", "oic",
        r"\b(book\w*|slots?|cancel\w*|reschedul\w*|waitlist\w*|wait list|queue|quota|limit|availability|calendar)\b",
        "Booking, slots and waitlist",
        "You can see, cancel or reschedule your bookings from View Booking (My Bookings) until the cutoff shown "
        "on each booking. We are checking your booking question and will update this ticket.",
    ),
    Topic(
        "portal_bug", "technical", "admin",
        r"\b(website|portal|page|button|not loading|blank|500|server error|bug|crash\w*|app|browser|screen)\b",
        "Portal problem",
        "Thank you for reporting this portal problem. Our team is looking into it; a screenshot and the page "
        "address help us fix it faster.",
    ),
    Topic(
        "suggestion", "quality_improvement", "admin",
        r"\b(suggest\w*|improve\w*|feature|would be (nice|better|great)|feedback|please add)\b",
        "Suggestion",
        "Thank you for the suggestion. We have recorded it for the portal team.",
    ),
)

_URGENT_RE = re.compile(
    r"\b(urgent\w*|asap|immediately|deadline|thesis|viva|defen[cs]e|stuck|charged twice|double (debit|charge)|"
    r"money deducted|not refunded|lost)\b",
    re.I,
)


@dataclass
class TriageResult:
    topic: str = ""
    ticket_type: str = ""
    priority: str = ""
    route_role: str = "admin"
    equipment: Any = None
    assignee: Any = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    reply_draft: str = ""
    draft_sources: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        eq = self.equipment
        who = self.assignee
        return {
            "topic": self.topic,
            "category": self.ticket_type,
            "priority": self.priority,
            "route_role": self.route_role,
            "equipment": {"equipment_id": int(eq.pk), "name": eq.name, "code": eq.code or ""} if eq else None,
            "suggested_assignee": (
                {"id": int(who.pk), "name": who.get_display_name() if hasattr(who, "get_display_name") else str(who.pk)}
                if who else None
            ),
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "reply_draft": self.reply_draft,
            "draft_sources": self.draft_sources,
        }


def _text(ticket) -> str:
    return " ".join(f"{ticket.subject or ''} {ticket.description or ''}".split())[:4000]


def detect_equipment(text: str):
    """The single instrument the text points at (code or full name, or a technique only one instrument has)."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.research_copilot.services.assistant import matching

    lower = text.lower()
    compact = matching.compact(lower)
    words = set(re.findall(r"[a-z0-9-]+", lower))
    feats = [matching.features(eq) for eq in Equipment.objects.select_related("category").all()[:2000]]
    hits = []
    for f in feats:
        code = (f.eq.code or "").lower()
        if code and len(code) >= 3 and (code in words or (len(f.code_c) >= 5 and f.code_c in compact)):
            hits.append((1.0, f, "code"))
        elif len(f.name_c) >= 8 and f.name_c in compact:
            hits.append((0.9, f, "name"))
    if not hits:
        techs = matching.techniques_in(lower)
        if techs:
            with_tech = [f for f in feats if f.techniques & techs and f.active and not f.parent_id]
            if len(with_tech) == 1:
                hits.append((0.75, with_tech[0], "technique"))
    if not hits:
        return None, ""
    hits.sort(key=lambda h: -h[0])
    top = hits[0]
    if len(hits) > 1 and hits[1][0] == top[0] and hits[1][1].eq.pk != top[1].eq.pk:
        return None, ""
    return top[1].eq, top[2]


def _primary_operator(equipment):
    from iic_booking.equipment.models import EquipmentOperator

    row = (
        EquipmentOperator.objects.filter(equipment=equipment, operator__is_active=True)
        .select_related("operator")
        .order_by("pk")
        .first()
    )
    return row.operator if row else None


def _article_draft(text: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        from iic_booking.research_copilot.models import CopilotKnowledgeArticle, KnowledgeArticleStatus
    except ImportError:
        return "", []
    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
    if not words:
        return "", []
    best, best_score = None, 0
    for art in CopilotKnowledgeArticle.objects.filter(status=KnowledgeArticleStatus.APPROVED)[:300]:
        keys = {str(k).lower() for k in (art.keywords or [])}
        title_words = set(re.findall(r"[a-z]{4,}", f"{art.title} {art.question}".lower()))
        score = 2 * len(words & keys) + len(words & title_words)
        if score > best_score:
            best, best_score = art, score
    if best is None or best_score < 3:
        return "", []
    return best.answer.strip(), [{"type": "copilot_answer", "id": str(best.pk), "title": best.title}]


def triage(ticket) -> TriageResult:
    from iic_booking.support.ticket_service import get_equipment_primary_oic

    text = _text(ticket)
    lower = text.lower()
    res = TriageResult()
    scores: list[tuple[int, Topic]] = []
    for i, topic in enumerate(TOPICS):
        n = len(re.findall(topic.pattern, lower))
        if n:
            scores.append((n, topic))
    chosen = None
    if scores:
        scores.sort(key=lambda s: (-s[0], TOPICS.index(s[1])))
        chosen = scores[0][1]
        res.reasons.append(f"Mentions {chosen.label.lower()}")
        res.confidence = min(0.5 + 0.1 * scores[0][0], 0.8)
        if len(scores) > 1 and scores[1][0] == scores[0][0]:
            res.confidence -= 0.15
    if chosen is None:
        res.ticket_type = ticket.ticket_type or "general"
        res.topic = "general"
        res.confidence = 0.3
        res.reasons.append("No clear topic; left with the support team")
    else:
        res.topic, res.ticket_type, res.route_role = chosen.key, chosen.ticket_type, chosen.route

    eq = ticket.related_equipment if getattr(ticket, "related_equipment_id", None) else None
    how = "linked on the ticket" if eq else ""
    if eq is None:
        eq, how = detect_equipment(text)
    res.equipment = eq
    if eq is not None:
        res.reasons.append(f"Equipment {eq.code or eq.name} ({how})")
        res.confidence = min(1.0, res.confidence + 0.15)
        if res.route_role == "admin" and res.topic not in ("payment", "account", "portal_bug", "suggestion"):
            res.route_role = "oic"
        if chosen is None:
            res.route_role, res.ticket_type = "oic", "equipment"
    if res.route_role in ("oic", "operator") and eq is not None:
        person = _primary_operator(eq) if res.route_role == "operator" else None
        if person is None:
            person = get_equipment_primary_oic(eq)
            res.route_role = "oic" if person else res.route_role
        if person is None and res.route_role == "oic":
            person = _primary_operator(eq)
            res.route_role = "operator" if person else "admin"
        res.assignee = person
    elif res.route_role in ("oic", "operator"):
        res.route_role = "admin"
        res.reasons.append("No equipment identified, so the support team handles it")
        res.confidence = min(res.confidence, 0.6)

    urgent = _URGENT_RE.search(lower)
    if urgent:
        res.priority = "high"
        res.reasons.append(f"Urgency words (\u201c{urgent.group(0)}\u201d)")
    else:
        res.priority = ticket.priority or "medium"

    draft, sources = _article_draft(text)
    body = draft or (chosen.draft if chosen else "We have received your ticket and will reply here soon.")
    if not sources and chosen:
        sources = [{"type": "template", "id": chosen.key, "title": chosen.label}]
    name = ticket.get_user_name() if hasattr(ticket, "get_user_name") else ""
    greeting = f"Dear {name}," if name and name != "Anonymous" else "Hello,"
    res.reply_draft = f"{greeting}\n\n{body}\n\nRegards,\nIIC Support Team"
    res.draft_sources = sources
    res.confidence = round(max(0.0, min(res.confidence, 1.0)), 2)
    return res


def save_triage(ticket, *, actor=None, dry_run: bool = False):
    """Store (or refresh) the suggestion for a ticket; auto-assign when that switch is on."""
    from .audit import log_action, settings_row
    from .models import TicketTriage

    res = triage(ticket)
    if dry_run:
        return res, None
    row, _ = TicketTriage.objects.update_or_create(
        ticket=ticket,
        defaults={
            "category": res.ticket_type,
            "topic": res.topic,
            "priority": res.priority,
            "route_role": res.route_role,
            "suggested_assignee": res.assignee,
            "equipment": res.equipment,
            "confidence": res.confidence,
            "reasons": res.reasons,
            "reply_draft": res.reply_draft,
            "draft_sources": res.draft_sources,
        },
    )
    log_action(
        "ticket_triage", "suggested", target_type="ticket", target_id=ticket.pk, actor=actor,
        summary=f"{res.ticket_type} / {res.route_role} ({res.confidence:.2f})",
        detail={"topic": res.topic, "priority": res.priority, "assignee_id": getattr(res.assignee, "pk", None),
                "equipment_id": getattr(res.equipment, "pk", None)},
    )
    cfg = settings_row()
    if (
        cfg.ticket_auto_assign_enabled
        and res.assignee is not None
        and res.confidence >= AUTO_ASSIGN_MIN_CONFIDENCE
    ):
        _auto_assign(ticket, res, row)
    return res, row


def _auto_assign(ticket, res: TriageResult, row) -> None:
    from iic_booking.support.models import Ticket, TicketEvent
    from iic_booking.support.ticket_service import notify_ticket_assignee, record_ticket_event

    from .audit import log_action

    updated = Ticket.objects.filter(pk=ticket.pk, assigned_to__isnull=True).update(
        assigned_to=res.assignee, updated_at=timezone.now()
    )
    if not updated:
        log_action("ticket_assign", "skipped", target_type="ticket", target_id=ticket.pk, summary="already assigned")
        return
    ticket.refresh_from_db()
    who = res.assignee.get_display_name() if hasattr(res.assignee, "get_display_name") else str(res.assignee.pk)
    record_ticket_event(
        ticket,
        TicketEvent.EventType.ASSIGNED,
        message=f"Auto-assigned to {who} by ticket triage.",
        from_value="Unassigned",
        to_value=who,
        metadata={"auto": True, "reason": "triage", "confidence": res.confidence},
        is_internal=True,
    )
    notify_ticket_assignee(ticket)
    log_action(
        "ticket_assign", "assigned", target_type="ticket", target_id=ticket.pk, recipient=res.assignee,
        summary=f"auto-assigned ({res.route_role}, {res.confidence:.2f})",
    )


def on_ticket_created(ticket_id: int) -> None:
    from iic_booking.support.models import Ticket

    from .audit import settings_row

    if not settings_row().ticket_triage_enabled:
        return
    ticket = Ticket.objects.select_related("related_equipment", "user").filter(pk=ticket_id).first()
    if ticket is None:
        return
    try:
        save_triage(ticket)
    except Exception:  # noqa: BLE001
        logger.exception("ticket triage failed ticket_id=%s", ticket_id)


def schedule_triage(ticket_id: int) -> None:
    transaction.on_commit(lambda: on_ticket_created(ticket_id))


def apply_decision(row, *, user, accept: bool, apply_type: bool = True, apply_priority: bool = True,
                   assign: bool = True) -> None:
    """Accept (apply the suggested type / priority / handler) or dismiss a suggestion. Never sends the draft."""
    from iic_booking.support.models import Ticket, TicketEvent
    from iic_booking.support.ticket_service import notify_ticket_assignee, record_ticket_event

    from .audit import log_action

    ticket = row.ticket
    changes: dict[str, Any] = {}
    if accept:
        fields = []
        if apply_type and row.category and row.category != ticket.ticket_type:
            changes["ticket_type"] = [ticket.ticket_type, row.category]
            ticket.ticket_type = row.category
            fields.append("ticket_type")
        if apply_priority and row.priority and row.priority != ticket.priority:
            changes["priority"] = [ticket.priority, row.priority]
            ticket.priority = row.priority
            fields.append("priority")
        newly_assigned = False
        if assign and row.suggested_assignee_id and ticket.assigned_to_id != row.suggested_assignee_id:
            changes["assigned_to"] = [ticket.assigned_to_id, row.suggested_assignee_id]
            ticket.assigned_to_id = row.suggested_assignee_id
            fields.append("assigned_to")
            newly_assigned = True
        if fields:
            ticket.save(update_fields=[*fields, "updated_at"])
            record_ticket_event(
                ticket,
                TicketEvent.EventType.ASSIGNED if newly_assigned else TicketEvent.EventType.PRIORITY_CHANGED,
                actor=user,
                message="Triage suggestion accepted.",
                metadata={"triage": True, "changes": {k: [str(a), str(b)] for k, (a, b) in changes.items()}},
                is_internal=True,
            )
            if newly_assigned:
                ticket = Ticket.objects.select_related("assigned_to", "related_equipment").get(pk=ticket.pk)
                notify_ticket_assignee(ticket, assigned_by=user)
    row.status = row.Status.ACCEPTED if accept else row.Status.DISMISSED
    row.decided_by = user
    row.decided_at = timezone.now()
    row.save(update_fields=["status", "decided_by", "decided_at"])
    log_action(
        "ticket_triage", "accepted" if accept else "dismissed", target_type="ticket", target_id=ticket.pk,
        actor=user, summary=", ".join(changes) or "no field changes",
        detail={k: [str(a), str(b)] for k, (a, b) in changes.items()},
    )
