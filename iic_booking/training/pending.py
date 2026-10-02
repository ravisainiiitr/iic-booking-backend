"""Training items for the pending-actions popup. Silent when the module is off."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import access
from .models import (
    AppealStatus,
    CallStatus,
    DemoRequest,
    DemoStatus,
    NominationCall,
    NominationStatus,
    RunStatus,
    SelectionAppeal,
    SessionStatus,
    TrainingNomination,
    TrainingSession,
)

ATTENDANCE_GRACE = timedelta(hours=1)


def _scope(qs, ids: set[int] | None, field: str = "equipment_id"):
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


def training_items(c) -> None:
    if not access.module_enabled() or not access.in_audience(c.user):
        return
    _personal(c)
    _staff(c)


def _personal(c) -> None:
    user = c.user
    now = timezone.now()
    c.add(
        "training_confirm_interest",
        "Confirm your training nomination",
        TrainingNomination.objects.filter(
            student=user,
            status=NominationStatus.SUBMITTED,
            student_confirmed_at__isnull=True,
            call__status=CallStatus.OPEN,
            call__deadline__gt=now,
        ).select_related("call"),
        "/my-trainings?tab=applications",
        "Your supervisor nominated you for a training. Confirm your interest so you are considered.",
        lambda n: f"{n.call.title} — by {timezone.localtime(n.call.deadline):%d %b %Y}",
    )
    c.add(
        "training_confirm_seat",
        "Confirm your training seat",
        TrainingNomination.objects.filter(student=user, status=NominationStatus.SELECTED, confirm_deadline__gt=now).select_related("call"),
        "/my-trainings?tab=applications",
        "You have been offered a training seat. Accept or decline it before the deadline.",
        lambda n: f"{n.call.title} — confirm by {timezone.localtime(n.confirm_deadline):%d %b %H:%M}",
    )
    c.add(
        "training_demo_proposals",
        "Respond to a proposed demonstration time",
        DemoRequest.objects.filter(requester=user, status=DemoStatus.PROPOSED_ALTERNATIVE).select_related("equipment"),
        "/training/demo-requests",
        "The OIC proposed another time for your demonstration request. Accept it or counter once.",
        lambda r: f"{r.reference} — {r.equipment.name}",
    )


def _staff(c) -> None:
    user = c.user
    now = timezone.now()
    admin = access.is_admin(user)
    oic_ids: set[int] | None = None if admin else access.oic_equipment_ids(user)
    dept_ids = set() if admin else access.dept_admin_equipment_ids(user)
    op_ids = set() if admin else access.operator_equipment_ids(user)

    if admin or oic_ids:
        c.add(
            "training_demo_requests",
            "Demonstration requests to review",
            _scope(DemoRequest.objects.filter(status__in=(DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW)), oic_ids)
            .select_related("equipment", "requester"),
            "/training/oic?tab=requests",
            "Faculty requested equipment demonstrations. Approve, curtail, propose another time or reject.",
            lambda r: f"{r.reference} — {r.equipment.name} ({r.course_code or r.get_purpose_display()})",
        )
        c.add(
            "training_demo_to_schedule",
            "Approved demonstrations to schedule",
            _scope(DemoRequest.objects.filter(status=DemoStatus.APPROVED), oic_ids).select_related("equipment"),
            "/training/oic?tab=requests",
            "These demonstrations are approved but no instrument time is reserved yet.",
            lambda r: f"{r.reference} — {r.equipment.name}",
        )
        c.add(
            "training_shortlist_publish",
            "Training shortlists to publish",
            _scope(
                NominationCall.objects.filter(Q(status=CallStatus.CLOSED) | Q(status=CallStatus.OPEN, deadline__lte=now)),
                oic_ids,
            ),
            "/training/oic?tab=calls",
            "Nominations have closed. Review the shortlist and publish the selection.",
            lambda call: f"{call.reference} — {call.title}",
        )

    appeal_ids = None if admin else (oic_ids or set()) | dept_ids
    if admin or appeal_ids:
        c.add(
            "training_appeals",
            "Training selection appeals",
            _scope(
                SelectionAppeal.objects.filter(status=AppealStatus.PENDING, entry__run__status=RunStatus.PUBLISHED),
                appeal_ids,
                "entry__run__call__equipment_id",
            ).select_related("entry__run__call"),
            "/training/oic?tab=calls",
            "Students or faculty appealed a published selection. Decide each appeal.",
            lambda a: f"{a.entry.run.call.reference} — {a.entry.run.call.title}",
        )
    if admin or dept_ids:
        c.add(
            "training_demo_escalated",
            "Overdue demonstration requests",
            _scope(
                DemoRequest.objects.filter(status__in=(DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW), sla_escalated_at__isnull=False),
                None if admin else dept_ids,
            ).select_related("equipment"),
            "/training/oic?tab=requests",
            "These demonstration requests passed the review deadline without an OIC decision.",
            lambda r: f"{r.reference} — {r.equipment.name}",
        )

    attend_ids = None if admin else (oic_ids or set()) | op_ids
    if admin or attend_ids:
        sessions = TrainingSession.objects.filter(
            status__in=(SessionStatus.PLANNED, SessionStatus.SCHEDULED),
            end_at__lte=now - ATTENDANCE_GRACE,
        ).exclude(event__kind="DEMO")
        if attend_ids is not None:
            sessions = sessions.filter(Q(equipment_id__in=attend_ids) | Q(equipment__isnull=True, event__equipment_id__in=attend_ids))
        c.add(
            "training_attendance",
            "Training attendance to mark",
            sessions.select_related("event").order_by("end_at"),
            "/training/attendance" if op_ids and not oic_ids else "/training/oic?tab=attendance",
            "These training sessions are over. Mark attendance so certificates can be issued.",
            lambda s: f"{s.event.title} — {timezone.localtime(s.start_at):%d %b %H:%M}",
        )
