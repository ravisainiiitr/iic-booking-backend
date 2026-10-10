"""
Active operator roster per equipment.

Members come from (a) an active certification at Certified operator level or above on the equipment, (b) an
approved legacy TA operating nomination for a semester that has not ended (backward compatibility with the TA
nomination flow), or (c) a manual addition by the OIC with a reason. ``sync`` adds missing members and refreshes
their basis; it never deletes rows, so pauses, removals and notes survive. Duty eligibility is computed on read.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import access
from .audit import audit
from .certification import OPERATOR_RANK
from .errors import TrainingError
from .models import AwardStatus, CertificationAward, OperatorRosterEntry, RosterSource, RosterStatus


def _legacy_qs(equipment_ids):
    from iic_booking.equipment.models import StudentEquipmentNomination, StudentEquipmentNominationStatus

    today = timezone.localdate()
    return StudentEquipmentNomination.objects.filter(
        equipment_id__in=equipment_ids,
        status=StudentEquipmentNominationStatus.APPROVED,
        semester__is_active=True,
        semester__end_date__gte=today,
    ).select_related("student", "semester", "supervisor")


def _award_qs(equipment_ids):
    return CertificationAward.objects.filter(
        equipment_id__in=equipment_ids, status=AwardStatus.ACTIVE, level__rank__gte=OPERATOR_RANK
    ).select_related("user", "level")


def sync(equipment_ids) -> int:
    equipment_ids = list(equipment_ids)
    if not equipment_ids:
        return 0
    existing = {(e.equipment_id, e.user_id): e for e in OperatorRosterEntry.objects.filter(equipment_id__in=equipment_ids)}
    created = 0
    with transaction.atomic():
        for award in _award_qs(equipment_ids):
            key = (award.equipment_id, award.user_id)
            entry = existing.get(key)
            if entry is None:
                existing[key] = OperatorRosterEntry.objects.create(
                    equipment_id=award.equipment_id,
                    user=award.user,
                    source=RosterSource.AWARD,
                    award=award,
                    faculty_id=award.user.supervisor_id,
                    department_id=award.user.department_id,
                )
                created += 1
            elif entry.award_id != award.pk:
                entry.award = award
                entry.source = RosterSource.AWARD
                entry.save(update_fields=["award", "source", "updated_at"])
        for nom in _legacy_qs(equipment_ids):
            key = (nom.equipment_id, nom.student_id)
            entry = existing.get(key)
            if entry is None:
                from iic_booking.equipment.academic_years import label_from_semester

                existing[key] = OperatorRosterEntry.objects.create(
                    equipment_id=nom.equipment_id,
                    user=nom.student,
                    source=RosterSource.LEGACY_TA,
                    legacy_nomination=nom,
                    faculty_id=nom.supervisor_id,
                    department_id=nom.student.department_id,
                    note=f"Approved TA nomination ({label_from_semester(nom.semester)})",
                )
                created += 1
            elif entry.legacy_nomination_id is None:
                entry.legacy_nomination = nom
                entry.save(update_fields=["legacy_nomination", "updated_at"])
    return created


def basis(entry: OperatorRosterEntry) -> tuple[bool, str]:
    """Whether the member currently qualifies for duty, and a plain-language reason."""
    user = entry.user
    if not user.is_active or getattr(user, "force_inactive", False):
        return False, "Account inactive"
    if entry.status == RosterStatus.PAUSED:
        return False, f"Paused by the OIC{': ' + entry.status_reason if entry.status_reason else ''}"
    if entry.status == RosterStatus.REMOVED:
        return False, "Removed from the roster"
    award = (
        CertificationAward.objects.filter(user_id=entry.user_id, equipment_id=entry.equipment_id, level__rank__gte=OPERATOR_RANK)
        .select_related("level")
        .order_by("-level__rank", "-awarded_at")
        .first()
    )
    now = timezone.now()
    if award and award.status == AwardStatus.ACTIVE and (not award.valid_until or award.valid_until >= now):
        return True, f"{award.level.name}" + (f" until {timezone.localtime(award.valid_until):%d %b %Y}" if award.valid_until else "")
    if entry.legacy_nomination_id and _legacy_qs([entry.equipment_id]).filter(pk=entry.legacy_nomination_id).exists():
        return True, "Approved TA nomination for the current academic year"
    if entry.source == RosterSource.MANUAL:
        return True, "Added by the OIC"
    if award and award.status in (AwardStatus.DORMANT, AwardStatus.SUSPENDED, AwardStatus.EXPIRED):
        return False, f"Certification {award.get_status_display().lower()} — refresher or renewal needed"
    return False, "No active certification or approved TA nomination"


def require_manager(equipment_id: int, actor) -> None:
    if not access.can_manage_equipment(actor, equipment_id):
        raise TrainingError("Only the equipment's OIC can manage its operator roster.", status=403, code="forbidden")


def add_manual(equipment, actor, *, user, reason: str) -> OperatorRosterEntry:
    require_manager(equipment.equipment_id, actor)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required to add an operator manually.", code="reason_required")
    if user.user_type not in access.STUDENT_TYPES:
        raise TrainingError("Only students (research scholars / TAs) can be added to the operator roster.")
    entry, created = OperatorRosterEntry.objects.get_or_create(
        equipment=equipment,
        user=user,
        defaults={
            "source": RosterSource.MANUAL,
            "faculty_id": user.supervisor_id,
            "department_id": user.department_id,
            "note": reason,
            "added_by": actor,
        },
    )
    if not created:
        if entry.status == RosterStatus.ACTIVE:
            raise TrainingError("This person is already on the roster.")
        entry.status = RosterStatus.ACTIVE
        entry.status_reason = ""
        entry.note = reason
        entry.save(update_fields=["status", "status_reason", "note", "updated_at"])
    audit(actor, "roster.added", entry, after={"user": user.pk, "equipment": equipment.pk}, note=reason)
    return entry


def set_status(entry: OperatorRosterEntry, actor, action: str, reason: str) -> OperatorRosterEntry:
    require_manager(entry.equipment_id, actor)
    reason = (reason or "").strip()
    target = {"pause": RosterStatus.PAUSED, "resume": RosterStatus.ACTIVE, "remove": RosterStatus.REMOVED}.get(action)
    if target is None:
        raise TrainingError("Unknown action.", status=404)
    if target != RosterStatus.ACTIVE and not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    before = entry.status
    entry.status = target
    entry.status_reason = reason
    entry.save(update_fields=["status", "status_reason", "updated_at"])
    audit(actor, f"roster.{action}", entry, before={"status": before}, after={"status": target}, note=reason)
    return entry


def update(entry: OperatorRosterEntry, actor, data: dict) -> OperatorRosterEntry:
    require_manager(entry.equipment_id, actor)
    fields = []
    if "max_hours_week" in data:
        raw = data.get("max_hours_week")
        if raw in (None, ""):
            entry.max_hours_week = None
        else:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise TrainingError("max_hours_week must be a whole number.") from None
            if not 1 <= v <= 80:
                raise TrainingError("max_hours_week must be between 1 and 80.")
            entry.max_hours_week = v
        fields.append("max_hours_week")
    if "note" in data:
        entry.note = (data.get("note") or "").strip()
        fields.append("note")
    if fields:
        entry.save(update_fields=[*fields, "updated_at"])
        audit(actor, "roster.updated", entry, after={f: getattr(entry, f) for f in fields})
    return entry


def entries(equipment_ids, *, include_removed: bool = False):
    qs = OperatorRosterEntry.objects.filter(equipment_id__in=list(equipment_ids)).select_related(
        "user", "user__department", "faculty", "department", "equipment", "award", "award__level"
    )
    if not include_removed:
        qs = qs.filter(~Q(status=RosterStatus.REMOVED))
    return qs
