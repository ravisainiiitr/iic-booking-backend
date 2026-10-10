"""
Competency assessment, tiered certification and certificate lifecycle.

Tiers (``CertificationLevel.rank``): Orientation (5) → Trained / supervised user (10) → Certified operator L1 (20,
independent use and operator duty) → Advanced operator / trainer L2 (30). An assessment records the theory score
and the practical checklist; every critical item must pass and the practical and theory scores must reach the
checklist's pass marks. The OIC signs off; when the assessor is the OIC the sign-off is immediate. A passing,
signed-off assessment issues the award (certificate number, validity, verification token) and supersedes lower
levels on the same equipment. Validity ends in EXPIRED; certified operators unused for the dormancy period become
DORMANT until a refresher (reinstate or renew).
"""

from __future__ import annotations

import io
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from iic_booking.users.display import get_user_display_name

from . import access, notify
from .audit import audit
from .errors import TrainingError
from .models import (
    DEFAULT_CHECKLIST_ITEMS,
    Assessment,
    AssessmentResult,
    AwardStatus,
    BadgeDefinition,
    CertificationAward,
    CertificationLevel,
    CompetencyChecklist,
    Registration,
    RegistrationStatus,
    TrainingAuditLog,
    UserBadge,
)
from .policy import effective_policy

logger = logging.getLogger(__name__)

HELD = (AwardStatus.ACTIVE, AwardStatus.PROVISIONAL, AwardStatus.DORMANT)
OPERATOR_RANK = 20
MAX_VALIDITY_MONTHS = 60


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------
def checklist_for(equipment) -> CompetencyChecklist:
    row = CompetencyChecklist.objects.filter(equipment=equipment).first() if equipment is not None else None
    if row is None:
        row = CompetencyChecklist.objects.filter(equipment__isnull=True).first()
    if row is None:
        row = CompetencyChecklist(items=DEFAULT_CHECKLIST_ITEMS)
    if not row.items:
        row.items = DEFAULT_CHECKLIST_ITEMS
    return row


def checklist_out(equipment) -> dict:
    row = checklist_for(equipment)
    return {
        "equipment_id": getattr(equipment, "equipment_id", None),
        "is_default": row.equipment_id is None,
        "items": row.items,
        "theory_pass_pct": row.theory_pass_pct,
        "practical_pass_pct": row.practical_pass_pct,
        "updated_at": row.updated_at.isoformat() if row.pk and row.updated_at else None,
    }


def save_checklist(equipment, actor, data: dict) -> dict:
    if not access.can_manage_equipment(actor, equipment.equipment_id):
        raise TrainingError("Only the equipment's OIC can edit its competency checklist.", status=403, code="forbidden")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise TrainingError("Add at least one checklist item.")
    items, keys = [], set()
    for i, item in enumerate(raw_items[:40]):
        label = str((item or {}).get("label") or "").strip()[:200]
        if not label:
            continue
        key = str(item.get("key") or "").strip()[:40] or f"item{i + 1}"
        while key in keys:
            key = f"{key}_{i + 1}"
        keys.add(key)
        items.append({"key": key, "label": label, "critical": bool(item.get("critical"))})
    if not items:
        raise TrainingError("Add at least one checklist item.")
    values = {"items": items, "updated_by": actor}
    for f in ("theory_pass_pct", "practical_pass_pct"):
        if data.get(f) not in (None, ""):
            try:
                v = int(data[f])
            except (TypeError, ValueError):
                raise TrainingError(f"{f} must be a whole number.") from None
            if not 0 <= v <= 100:
                raise TrainingError(f"{f} must be between 0 and 100.")
            values[f] = v
    row, _ = CompetencyChecklist.objects.update_or_create(equipment=equipment, defaults=values)
    audit(actor, "checklist.saved", row, after={"items": len(items)})
    return checklist_out(equipment)


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------
def can_assess(user, equipment_id: int) -> bool:
    if access.can_manage_equipment(user, equipment_id) or equipment_id in access.operator_equipment_ids(user):
        return True
    return CertificationAward.objects.filter(
        user=user, equipment_id=equipment_id, status=AwardStatus.ACTIVE, level__rank__gte=30
    ).exists()


def _pct(value, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise TrainingError(f"{field} must be a number.") from None
    if v < 0 or v > 100:
        raise TrainingError(f"{field} must be between 0 and 100.")
    return v


def grade(checklist: CompetencyChecklist, theory: Decimal | None, marks: list[dict]) -> tuple[list[dict], Decimal | None, str, list[str]]:
    """Merge marks into the checklist and decide PASS / RETAKE / FAIL. Pure apart from reading the checklist."""
    by_key = {str(m.get("key")): m for m in marks or [] if isinstance(m, dict)}
    items, reasons = [], []
    passed = total = 0
    critical_failed = []
    unmarked = []
    for item in checklist.items:
        mark = by_key.get(item["key"], {})
        ok = mark.get("passed")
        ok = None if ok is None else bool(ok)
        items.append({**item, "passed": ok, "note": str(mark.get("note") or "")[:300]})
        if ok is None:
            unmarked.append(item["label"])
            continue
        total += 1
        passed += 1 if ok else 0
        if item.get("critical") and not ok:
            critical_failed.append(item["label"])
    if unmarked:
        raise TrainingError("Mark every checklist item as passed or not passed.", extra={"unmarked": unmarked})
    practical = (Decimal(passed * 100) / Decimal(total)).quantize(Decimal("0.01")) if total else None
    if critical_failed:
        reasons.append("Critical item not passed: " + "; ".join(critical_failed))
    if practical is not None and practical < checklist.practical_pass_pct:
        reasons.append(f"Practical {practical}% is below the pass mark of {checklist.practical_pass_pct}%")
    if theory is not None and theory < checklist.theory_pass_pct:
        reasons.append(f"Theory {theory}% is below the pass mark of {checklist.theory_pass_pct}%")
    if not reasons:
        result = AssessmentResult.PASS
    elif critical_failed and practical is not None and practical < Decimal(checklist.practical_pass_pct) / 2:
        result = AssessmentResult.FAIL
    else:
        result = AssessmentResult.RETAKE
    return items, practical, result, reasons


def has_prerequisite(user, equipment, level: CertificationLevel) -> bool:
    if level.rank <= 10:
        return True
    from iic_booking.equipment.models import StudentEquipmentNomination, StudentEquipmentNominationStatus

    held = CertificationAward.objects.filter(user=user, equipment=equipment, status__in=HELD).exists()
    trained = Registration.objects.filter(
        user=user, event__equipment=equipment, status__in=(RegistrationStatus.COMPLETED, RegistrationStatus.ATTENDED)
    ).exists()
    legacy = StudentEquipmentNomination.objects.filter(
        student=user, equipment=equipment, status=StudentEquipmentNominationStatus.APPROVED
    ).exists()
    return held or trained or legacy


def record_assessment(actor, data: dict) -> Assessment:
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models import User

    equipment = Equipment.objects.filter(pk=data.get("equipment_id")).first()
    user = User.objects.filter(pk=data.get("user_id")).first()
    if equipment is None or user is None:
        raise TrainingError("Choose the equipment and the candidate.", status=404)
    if not can_assess(actor, equipment.equipment_id):
        raise TrainingError("Only the OIC, a Lab Operator or a trainer of this equipment can assess.", status=403, code="forbidden")
    if user.pk == actor.pk:
        raise TrainingError("You cannot assess yourself.", status=403, code="forbidden")
    level = CertificationLevel.objects.filter(code=data.get("target_level") or "CERT_L1", is_active=True).first()
    if level is None:
        raise TrainingError("Choose an active certification level.")
    waiver = (data.get("prerequisite_waiver_reason") or "").strip()
    if not has_prerequisite(user, equipment, level) and not waiver:
        raise TrainingError(
            "The candidate has no training, certification or approved TA nomination on this equipment. "
            "Give a reason to assess anyway (e.g. trained elsewhere).",
            code="prerequisite_missing",
        )
    months = data.get("validity_months")
    if months not in (None, ""):
        try:
            months = int(months)
        except (TypeError, ValueError):
            raise TrainingError("validity_months must be a whole number.") from None
        if not 1 <= months <= MAX_VALIDITY_MONTHS:
            raise TrainingError(f"Validity must be between 1 and {MAX_VALIDITY_MONTHS} months.")
    else:
        months = None
    checklist = checklist_for(equipment)
    theory = _pct(data.get("theory_score_pct"), "theory_score_pct")
    items, practical, result, reasons = grade(checklist, theory, data.get("practical_items") or [])
    registration = None
    if data.get("registration_id"):
        registration = Registration.objects.filter(pk=data["registration_id"], user=user).first()
    is_oic = access.can_manage_equipment(actor, equipment.equipment_id)
    now = timezone.now()
    with transaction.atomic():
        a = Assessment.objects.create(
            user=user,
            equipment=equipment,
            event=registration.event if registration else None,
            registration=registration,
            target_level=level,
            assessor=actor,
            theory_score_pct=theory,
            practical_items=items,
            practical_score_pct=practical,
            result=result,
            scope_note=(data.get("scope_note") or "").strip()[:2000],
            remarks=((data.get("remarks") or "").strip() + ("\n" + "\n".join(reasons) if reasons else "")).strip(),
            validity_months=months,
            prerequisite_waiver_reason=waiver,
            assessed_at=now,
        )
        audit(actor, "assessment.recorded", a, after={"result": result, "level": level.code, "user": user.pk}, note=waiver)
        if result == AssessmentResult.PASS and is_oic and data.get("issue_award", True):
            sign_off(a, actor)
    _notify_result(a, actor)
    return a


def sign_off(a: Assessment, actor) -> CertificationAward:
    if not access.can_manage_equipment(actor, a.equipment_id):
        raise TrainingError("Only the equipment's OIC can sign off a certification.", status=403, code="forbidden")
    if a.result != AssessmentResult.PASS:
        raise TrainingError("Only a passing assessment can be signed off.")
    if a.award_id:
        raise TrainingError("This assessment has already been signed off.")
    with transaction.atomic():
        award = issue(
            a.user,
            a.equipment,
            a.target_level,
            actor,
            months=a.validity_months,
            source_event=a.event,
            registration=a.registration,
        )
        a.award = award
        a.signed_off_by = actor
        a.signed_off_at = timezone.now()
        a.save(update_fields=["award", "signed_off_by", "signed_off_at"])
        audit(actor, "assessment.signed_off", a, after={"award": award.pk})
    return award


def validity_months_for(level: CertificationLevel, equipment, override: int | None = None) -> int | None:
    if override:
        return override
    if level.code == "TRAINED":
        return effective_policy(equipment).trained_validity_months
    return level.default_validity_months or effective_policy(equipment).trained_validity_months


def certificate_number(award: CertificationAward) -> str:
    code = (getattr(award.equipment, "code", "") or "GEN").upper().replace(" ", "")[:20]
    return f"IIC-{code}-{award.awarded_at.year}-{award.pk:05d}"


def issue(user, equipment, level: CertificationLevel, actor, *, months=None, source_event=None, registration=None) -> CertificationAward:
    existing = CertificationAward.objects.filter(user=user, equipment=equipment, status__in=HELD, level__rank__gte=level.rank).first()
    if existing:
        raise TrainingError(f"{get_user_display_name(user)} already holds {existing.level.name} on this equipment.")
    now = timezone.now()
    months = validity_months_for(level, equipment, months)
    award = CertificationAward.objects.create(
        user=user,
        equipment=equipment,
        equipment_group_id=getattr(equipment, "equipment_group_id", None),
        level=level,
        status=AwardStatus.ACTIVE,
        awarded_at=now,
        valid_until=now + relativedelta(months=int(months)) if months else None,
        source_event=source_event,
        source_registration=registration,
        awarded_by=actor if getattr(actor, "pk", None) else None,
    )
    award.certificate_no = certificate_number(award)
    award.save(update_fields=["certificate_no"])
    lower = CertificationAward.objects.filter(user=user, equipment=equipment, status__in=HELD, level__rank__lt=level.rank)
    for old in lower:
        audit(actor, "award.superseded", old, after={"by": award.pk})
    lower.update(status=AwardStatus.SUPERSEDED)
    badge = BadgeDefinition.objects.filter(level=level, is_active=True).first()
    if badge:
        UserBadge.objects.filter(user=user, equipment=equipment, revoked_at__isnull=True).update(revoked_at=now)
        UserBadge.objects.create(user=user, badge=badge, equipment=equipment, award=award, source="assessment", awarded_at=now)
    audit(actor, "award.issued", award, after={"level": level.code, "user": user.pk, "equipment": equipment.pk, "months": months})
    _notify_status(award, actor, "Issued", f"{get_user_display_name(user)} is now {level.name} on {equipment.name}"
                   + (f", valid until {notify.fmt_dt(award.valid_until)}." if award.valid_until else "."))
    return award


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------
def act(award: CertificationAward, actor, action: str, data: dict) -> CertificationAward:
    if not award.equipment_id or not access.can_manage_equipment(actor, award.equipment_id):
        raise TrainingError("Only the equipment's OIC can change this certification.", status=403, code="forbidden")
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise TrainingError("A reason is required.", code="reason_required")
    now = timezone.now()
    before = {"status": award.status, "valid_until": award.valid_until.isoformat() if award.valid_until else None}
    if action == "suspend":
        if award.status not in (AwardStatus.ACTIVE, AwardStatus.DORMANT):
            raise TrainingError("Only active or dormant certifications can be suspended.")
        award.status = AwardStatus.SUSPENDED
        award.suspended_at = now
        award.suspend_reason = reason
        until = data.get("until")
        award.suspended_until = None
        if until:
            from django.utils.dateparse import parse_datetime

            parsed = parse_datetime(str(until))
            if parsed and timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            award.suspended_until = parsed
        label = "Suspended"
    elif action == "reinstate":
        if award.status not in (AwardStatus.SUSPENDED, AwardStatus.DORMANT):
            raise TrainingError("Only suspended or dormant certifications can be reinstated.")
        if award.valid_until and award.valid_until < now:
            raise TrainingError("The validity has ended; renew it instead.")
        award.status = AwardStatus.ACTIVE
        award.last_used_at = now
        label = "Reinstated"
    elif action == "revoke":
        if award.status in (AwardStatus.REVOKED, AwardStatus.SUPERSEDED):
            raise TrainingError("This certification is already closed.")
        award.status = AwardStatus.REVOKED
        award.revoked_at = now
        award.revoked_by = actor
        award.revoke_reason = reason
        UserBadge.objects.filter(award=award, revoked_at__isnull=True).update(revoked_at=now)
        label = "Revoked"
    elif action == "renew":
        if award.status not in (AwardStatus.ACTIVE, AwardStatus.DORMANT, AwardStatus.EXPIRED):
            raise TrainingError("Only active, dormant or expired certifications can be renewed.")
        try:
            months = int(data.get("months") or validity_months_for(award.level, award.equipment) or 24)
        except (TypeError, ValueError):
            raise TrainingError("months must be a whole number.") from None
        if not 1 <= months <= MAX_VALIDITY_MONTHS:
            raise TrainingError(f"Renewal must be between 1 and {MAX_VALIDITY_MONTHS} months.")
        base = award.valid_until if award.valid_until and award.valid_until > now else now
        award.valid_until = base + relativedelta(months=months)
        award.status = AwardStatus.ACTIVE
        award.last_used_at = now
        label = "Renewed"
    else:
        raise TrainingError("Unknown action.", status=404)
    award.save()
    audit(actor, f"award.{action}", award, before=before, after={"status": award.status}, note=reason)
    _notify_status(award, actor, label, f"{award.level.name} on {getattr(award.equipment, 'name', '')}: {label.lower()}. Reason: {reason}")
    return award


# ---------------------------------------------------------------------------
# Housekeeping: expiry, reminders, dormancy
# ---------------------------------------------------------------------------
def housekeeping() -> dict:
    from . import operator_policy

    now = timezone.now()
    expired = 0
    for award in CertificationAward.objects.filter(status__in=(AwardStatus.ACTIVE, AwardStatus.DORMANT), valid_until__lt=now).select_related("user", "equipment", "level"):
        award.status = AwardStatus.EXPIRED
        award.save(update_fields=["status"])
        audit(None, "award.expired", award)
        _notify_status(award, None, "Expired", f"{award.level.name} on {getattr(award.equipment, 'name', '')} has expired. Ask the OIC about a refresher or reassessment.")
        expired += 1
    reminded = 0
    reminded_ids = set(
        TrainingAuditLog.objects.filter(action="award.expiry_reminded", object_type="CertificationAward").values_list("object_id", flat=True)
    )
    horizon = now + timedelta(days=180)
    for award in CertificationAward.objects.filter(status=AwardStatus.ACTIVE, valid_until__gte=now, valid_until__lte=horizon).select_related("user", "equipment", "level"):
        days = operator_policy.effective(award.equipment).expiry_reminder_days
        if not days or str(award.pk) in reminded_ids or award.valid_until > now + timedelta(days=days):
            continue
        audit(None, "award.expiry_reminded", award)
        recipients = [award.user, *_oics(award.equipment)]
        notify.send(
            "certification_expiring_email",
            recipients,
            context=_award_context(award, summary=f"{award.level.name} of {get_user_display_name(award.user)} on {getattr(award.equipment, 'name', '')} expires on {notify.fmt_dt(award.valid_until)}. Plan a refresher or reassessment.", deadline=notify.fmt_dt(award.valid_until)),
            title=f"Certification expiring: {getattr(award.equipment, 'name', '')}",
            message=f"{award.level.name} expires on {notify.fmt_dt(award.valid_until)}.",
            path="/my-trainings?tab=certifications",
            event="training.award.expiring",
        )
        reminded += 1
    dormant = 0
    for award in CertificationAward.objects.filter(status=AwardStatus.ACTIVE, level__rank__gte=OPERATOR_RANK).select_related("user", "equipment", "level"):
        months = effective_policy(award.equipment).dormancy_months
        last = award.last_used_at or award.awarded_at
        if months and last < now - relativedelta(months=int(months)):
            award.status = AwardStatus.DORMANT
            award.save(update_fields=["status"])
            audit(None, "award.dormant", award, note=f"Not used for {months} months")
            _notify_status(award, None, "Dormant", f"{award.level.name} on {getattr(award.equipment, 'name', '')} is dormant after {months} months without use. A refresher with the OIC reinstates it.")
            dormant += 1
    return {"expired": expired, "reminded": reminded, "dormant": dormant}


# ---------------------------------------------------------------------------
# Certificate PDF and public verification
# ---------------------------------------------------------------------------
def verify_path(award: CertificationAward) -> str:
    return f"/verify/certificate/{award.verify_token}"


def effective_status(award: CertificationAward) -> str:
    if award.status in (AwardStatus.ACTIVE, AwardStatus.DORMANT) and award.valid_until and award.valid_until < timezone.now():
        return AwardStatus.EXPIRED
    return award.status


def public_record(award: CertificationAward) -> dict:
    status = effective_status(award)
    scope = Assessment.objects.filter(award=award).values_list("scope_note", flat=True).first() or ""
    return {
        "certificate_no": award.certificate_no or "",
        "holder": get_user_display_name(award.user),
        "equipment": {"code": getattr(award.equipment, "code", ""), "name": getattr(award.equipment, "name", "")},
        "level": {"code": award.level.code, "name": award.level.name},
        "status": status,
        "status_label": AwardStatus(status).label if status in AwardStatus.values else status,
        "valid": status == AwardStatus.ACTIVE,
        "awarded_at": award.awarded_at.isoformat(),
        "valid_until": award.valid_until.isoformat() if award.valid_until else None,
        "scope": scope,
        "issuer": "Institute Instrumentation Centre, IIT Roorkee",
    }


def certificate_pdf(award: CertificationAward) -> bytes:
    import qrcode
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    if not award.certificate_no:
        award.certificate_no = certificate_number(award)
        award.save(update_fields=["certificate_no"])
    rec = public_record(award)
    url = notify.frontend_link(verify_path(award))
    buf = io.BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    c.setTitle(f"Certificate {rec['certificate_no']}")
    navy = colors.HexColor("#0b3d6e")
    c.setStrokeColor(navy)
    c.setLineWidth(4)
    c.rect(24, 24, width - 48, height - 48)
    c.setLineWidth(1)
    c.rect(32, 32, width - 64, height - 64)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - 80, "Indian Institute of Technology Roorkee")
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 102, "Institute Instrumentation Centre")
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(width / 2, height - 160, "Certificate of Competence")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 200, "This is to certify that")
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, height - 235, rec["holder"])
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 268, f"has been assessed and authorised as {rec['level']['name']}")
    c.drawCentredString(width / 2, height - 290, f"on {rec['equipment']['name']} ({rec['equipment']['code']})")
    if rec["scope"]:
        c.setFont("Helvetica-Oblique", 11)
        c.drawCentredString(width / 2, height - 314, f"Scope: {rec['scope'][:140]}")
    c.setFont("Helvetica", 11)
    issued = timezone.localtime(award.awarded_at).strftime("%d %b %Y")
    valid = timezone.localtime(award.valid_until).strftime("%d %b %Y") if award.valid_until else "No expiry"
    c.drawString(70, 120, f"Certificate No.: {rec['certificate_no']}")
    c.drawString(70, 102, f"Issued: {issued}    Valid until: {valid}")
    signer = get_user_display_name(award.awarded_by) if award.awarded_by_id else "Officer In Charge"
    c.drawString(70, 84, f"Authorised by: {signer} (OIC)")
    c.setFont("Helvetica", 8)
    c.drawString(70, 60, f"Verify at {url}")
    qr = qrcode.QRCode(box_size=4, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    qbuf = io.BytesIO()
    img.save(qbuf, format="PNG")
    qbuf.seek(0)
    c.drawImage(ImageReader(qbuf), width - 170, 56, width=110, height=110)
    if not rec["valid"]:
        c.saveState()
        c.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.25))
        c.setFont("Helvetica-Bold", 90)
        c.translate(width / 2, height / 2)
        c.rotate(25)
        c.drawCentredString(0, 0, rec["status_label"].split(" ")[0].upper())
        c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _oics(equipment) -> list:
    from iic_booking.communication.in_app import equipment_oic_users

    return equipment_oic_users(equipment)


def _award_context(award: CertificationAward, **extra) -> dict:
    return {
        "reference": award.certificate_no or f"A-{award.pk}",
        "equipment_name": getattr(award.equipment, "name", ""),
        "equipment_code": getattr(award.equipment, "code", ""),
        "title": f"{award.level.name} – {getattr(award.equipment, 'name', '')}",
        **extra,
    }


def _notify_status(award: CertificationAward, actor, label: str, summary: str) -> None:
    recipients = [award.user]
    if getattr(award.user, "supervisor_id", None):
        recipients.append(award.user.supervisor)
    notify.send(
        "certification_status_email",
        recipients,
        context=_award_context(award, summary=summary, status=label),
        title=f"Certification {label.lower()}: {getattr(award.equipment, 'name', '')}",
        message=summary,
        path="/my-trainings?tab=certifications",
        actor=actor,
        event=f"training.award.{label.lower()}",
    )


def _notify_result(a: Assessment, actor) -> None:
    label = AssessmentResult(a.result).label
    summary = f"Competency assessment for {a.target_level.name} on {a.equipment.name}: {label}."
    if a.result == AssessmentResult.PASS and not a.award_id:
        summary += " Awaiting the OIC's sign-off."
    recipients = [a.user]
    if getattr(a.user, "supervisor_id", None):
        recipients.append(a.user.supervisor)
    notify.send(
        "training_assessment_result_email",
        recipients,
        context={
            "reference": f"AS-{a.pk:04d}",
            "equipment_name": a.equipment.name,
            "equipment_code": a.equipment.code,
            "title": a.target_level.name,
            "status": label,
            "summary": summary,
            "remarks": a.remarks,
        },
        title=f"Assessment: {label}",
        message=summary,
        path="/my-trainings?tab=certifications",
        actor=actor,
        event="training.assessment.recorded",
    )
    if a.result == AssessmentResult.PASS and not a.award_id:
        oics = [u for u in _oics(a.equipment) if u.pk != getattr(actor, "pk", None)]
        notify.send(
            "training_assessment_result_email",
            oics,
            context={"reference": f"AS-{a.pk:04d}", "equipment_name": a.equipment.name, "equipment_code": a.equipment.code,
                     "title": a.target_level.name, "status": "Sign-off needed",
                     "summary": f"{get_user_display_name(a.user)} passed the {a.target_level.name} assessment by {get_user_display_name(actor)}. Sign off to issue the certificate."},
            title="Certification sign-off needed",
            message=f"{get_user_display_name(a.user)} passed the assessment on {a.equipment.name}. Sign off to issue the certificate.",
            path="/training/oic?tab=certifications",
            actor=actor,
            event="training.assessment.signoff_needed",
        )
