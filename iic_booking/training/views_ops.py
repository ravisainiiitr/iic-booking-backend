"""Training API: competency assessment and certificates, the operator roster, duty allocation, hours accounting and
the operator policy. Public endpoints (certificate verification, one-click duty response) need no login."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import wraps

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from iic_booking.users.display import get_user_display_name

from . import access, accounting, certification, duty, operator_policy, roster
from . import serializers as s
from .audit import audit
from .errors import TrainingError
from .models import (
    Assessment,
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    DutyAllocation,
    DutyShift,
    DutyStatus,
    OperatorPolicy,
    OperatorRosterEntry,
    PolicyScope,
    Registration,
    RegistrationStatus,
    RosterStatus,
    ShiftStatus,
    TrainingAuditLog,
)
from .views import MAX_LIST, _forbid, _get, _staff_scope, training_api


class PublicThrottle(AnonRateThrottle):
    rate = "120/hour"


def public_api(methods):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except TrainingError as exc:
                return Response({"detail": exc.message, "code": exc.code, **exc.extra}, status=exc.status)

        return api_view(methods)(
            authentication_classes([])(permission_classes([AllowAny])(throttle_classes([PublicThrottle])(inner)))
        )

    return deco


def _manager_scope(user) -> set[int] | None:
    """Equipment whose duty and certificates the user may see as a manager (None = all)."""
    if access.is_admin(user):
        return None
    return access.oic_equipment_ids(user) | access.dept_admin_equipment_ids(user)


def _require_scope(ids):
    if ids is not None and not ids:
        _forbid("Only the OIC, a department admin or the Main Admin can open this.")


def _equipment(pk):
    from iic_booking.equipment.models import Equipment

    return _get(Equipment, pk)


def _int(raw):
    return int(raw) if str(raw or "").isdigit() else None


# ---------------------------------------------------------------------------
# Levels, checklist, assessments
# ---------------------------------------------------------------------------
@training_api(["GET"])
def levels(request):
    return Response(
        {
            "results": [
                {
                    "code": lv.code,
                    "name": lv.name,
                    "rank": lv.rank,
                    "description": lv.description,
                    "default_validity_months": lv.default_validity_months,
                    "operator": lv.rank >= certification.OPERATOR_RANK,
                }
                for lv in CertificationLevel.objects.filter(is_active=True).order_by("rank")
            ]
        }
    )


@training_api(["GET", "PUT"])
def checklist(request, equipment_id: int):
    eq = _equipment(equipment_id)
    if request.method == "PUT":
        return Response(certification.save_checklist(eq, request.user, request.data))
    if not (certification.can_assess(request.user, eq.equipment_id) or access.can_view_equipment(request.user, eq.equipment_id)):
        _forbid()
    return Response(certification.checklist_out(eq))


@training_api(["GET", "POST"])
def assessments(request):
    user = request.user
    if request.method == "POST":
        a = certification.record_assessment(user, request.data)
        return Response(s.assessment_out(a), status=status.HTTP_201_CREATED)
    qs = Assessment.objects.select_related("user", "user__department", "equipment", "target_level", "assessor", "signed_off_by", "award")
    ids = _staff_scope(user)
    if ids is None:
        pass
    elif ids:
        qs = qs.filter(Q(equipment_id__in=ids) | Q(assessor=user))
    else:
        qs = qs.filter(Q(user=user) | Q(assessor=user))
    p = request.query_params
    if p.get("equipment_id"):
        qs = qs.filter(equipment_id=p["equipment_id"])
    if p.get("awaiting_sign_off") == "1":
        qs = qs.filter(result="PASS", award__isnull=True)
    if p.get("user_id"):
        qs = qs.filter(user_id=p["user_id"])
    return Response({"results": [s.assessment_out(a) for a in qs.order_by("-assessed_at")[:MAX_LIST]]})


@training_api(["POST"])
def assessment_sign_off(request, pk: int):
    a = _get(Assessment, pk)
    certification.sign_off(a, request.user)
    a.refresh_from_db()
    return Response(s.assessment_out(a))


@training_api(["GET"])
def assessment_candidates(request):
    """People with a basis to be assessed on the equipment: trained, attended training, TA nominees or holders."""
    from iic_booking.equipment.models import StudentEquipmentNomination, StudentEquipmentNominationStatus
    from iic_booking.users.models import User

    eq = _equipment(request.query_params.get("equipment_id"))
    if not certification.can_assess(request.user, eq.equipment_id):
        _forbid("Only the OIC, a Lab Operator or a trainer of this equipment can assess.")
    reasons: dict[int, list[str]] = {}

    def add(uid, why):
        reasons.setdefault(uid, [])
        if why not in reasons[uid]:
            reasons[uid].append(why)

    for r in Registration.objects.filter(
        event__equipment=eq, status__in=(RegistrationStatus.ATTENDED, RegistrationStatus.COMPLETED, RegistrationStatus.PARTIAL, RegistrationStatus.CONFIRMED)
    ).select_related("event"):
        add(r.user_id, f"{r.get_status_display()}: {r.event.title}")
    for n in StudentEquipmentNomination.objects.filter(equipment=eq, status=StudentEquipmentNominationStatus.APPROVED):
        add(n.student_id, "Approved TA nomination")
    held: dict[int, CertificationAward] = {}
    for a in CertificationAward.objects.filter(equipment=eq).exclude(status__in=(AwardStatus.REVOKED, AwardStatus.SUPERSEDED)).select_related("level"):
        cur = held.get(a.user_id)
        if cur is None or a.level.rank > cur.level.rank:
            held[a.user_id] = a
        add(a.user_id, f"Holds {a.level.name} ({a.get_status_display().lower()})")
    q = (request.query_params.get("q") or "").strip()
    users = User.objects.filter(id__in=list(reasons)).select_related("department").order_by("name")
    if q:
        users = users.filter(Q(name__icontains=q) | Q(email__icontains=q))
    pending = set(Assessment.objects.filter(equipment=eq, result="PASS", award__isnull=True).values_list("user_id", flat=True))
    return Response(
        {
            "results": [
                {
                    "user": s.user_brief(u),
                    "reasons": reasons.get(u.id, []),
                    "current_award": s.award_out(held[u.id]) if u.id in held else None,
                    "awaiting_sign_off": u.id in pending,
                }
                for u in users[:MAX_LIST]
            ]
        }
    )


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
def _award_visible(award: CertificationAward, user) -> bool:
    if award.user_id == user.pk or access.is_admin(user):
        return True
    if award.equipment_id and access.can_view_equipment(user, award.equipment_id):
        return True
    return access.is_faculty(user) and award.user_id in access.faculty_group_student_ids(user)


@training_api(["GET"])
def certification_detail(request, pk: int):
    award = _get(CertificationAward, pk)
    if not _award_visible(award, request.user):
        _forbid()

    history = TrainingAuditLog.objects.filter(object_type="CertificationAward", object_id=str(award.pk)).select_related("actor").order_by("created_at")
    return Response(
        {
            **s.award_out(award),
            "can_manage": bool(award.equipment_id) and access.can_manage_equipment(request.user, award.equipment_id),
            "assessments": [s.assessment_out(a) for a in award.assessments.select_related("user", "equipment", "target_level", "assessor", "signed_off_by", "award")],
            "history": [
                {
                    "action": h.action,
                    "at": h.created_at.isoformat(),
                    "by": s.user_brief(h.actor) if h.actor_id else None,
                    "note": h.note,
                }
                for h in history[:100]
            ],
        }
    )


@training_api(["POST"])
def certification_action(request, pk: int, action: str):
    award = _get(CertificationAward, pk)
    award = certification.act(award, request.user, action, request.data)
    return Response(s.award_out(award))


@training_api(["GET"])
def certificate_pdf(request, pk: int):
    award = _get(CertificationAward, pk)
    if not _award_visible(award, request.user):
        _forbid()
    pdf = certification.certificate_pdf(award)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{award.certificate_no or f"certificate-{award.pk}"}.pdf"'
    return resp


@public_api(["GET"])
def verify_certificate(request, token: str):
    award = CertificationAward.objects.filter(verify_token=token).select_related("user", "equipment", "level").first()
    if award is None:
        raise TrainingError("No certificate matches this verification code.", status=404, code="not_found")
    return Response(certification.public_record(award))


# ---------------------------------------------------------------------------
# Operator roster
# ---------------------------------------------------------------------------
@training_api(["GET", "POST"])
def roster_list(request):
    user = request.user
    if request.method == "POST":
        from iic_booking.users.models import User

        eq = _equipment(request.data.get("equipment_id"))
        person = _get(User, request.data.get("user_id"))
        entry = roster.add_manual(eq, user, user=person, reason=request.data.get("reason") or "")
        return Response(s.roster_out(entry), status=status.HTTP_201_CREATED)
    ids = _manager_scope(user)
    _require_scope(ids)
    eq_id = _int(request.query_params.get("equipment_id"))
    if eq_id is not None and ids is not None and eq_id not in ids:
        _forbid()
    target = [eq_id] if eq_id is not None else (list(ids) if ids is not None else list(access.pilot_equipment_queryset().values_list("equipment_id", flat=True)))
    roster.sync(target)
    qs = roster.entries(target, include_removed=request.query_params.get("include_removed") == "1").order_by("equipment__name", "user__name")
    return Response({"results": [s.roster_out(e) for e in qs[:MAX_LIST * 2]]})


@training_api(["PATCH"])
def roster_detail(request, pk: int):
    entry = _get(OperatorRosterEntry, pk)
    entry = roster.update(entry, request.user, request.data)
    return Response(s.roster_out(entry))


@training_api(["POST"])
def roster_action(request, pk: int, action: str):
    entry = _get(OperatorRosterEntry, pk)
    entry = roster.set_status(entry, request.user, action, request.data.get("reason") or "")
    return Response(s.roster_out(entry))


@training_api(["GET"])
def roster_people(request):
    """Students the OIC can add to the roster manually (search by name or email)."""
    from iic_booking.users.models import User

    eq = _equipment(request.query_params.get("equipment_id"))
    roster.require_manager(eq.equipment_id, request.user)
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": []})
    qs = (
        User.objects.filter(is_active=True, user_type__in=access.STUDENT_TYPES)
        .filter(Q(name__icontains=q) | Q(email__icontains=q))
        .select_related("department")
        .order_by("name")[:20]
    )
    return Response({"results": [s.user_brief(u) for u in qs]})


# ---------------------------------------------------------------------------
# Duty allocation
# ---------------------------------------------------------------------------
@training_api(["POST"])
def duty_plan(request):
    return Response(duty.plan_out(duty.plan(request.user, request.data)))


def _alloc_qs():
    return DutyAllocation.objects.select_related("equipment", "equipment__internal_department", "operator", "operator__department", "allocated_by")


@training_api(["GET", "POST"])
def duty_allocations(request):
    user = request.user
    if request.method == "POST":
        alloc = duty.create(user, request.data)
        return Response(s.allocation_out(alloc, user), status=status.HTTP_201_CREATED)
    ids = _manager_scope(user)
    _require_scope(ids)
    qs = _alloc_qs() if ids is None else _alloc_qs().filter(equipment_id__in=ids)
    p = request.query_params
    if p.get("equipment_id"):
        qs = qs.filter(equipment_id=p["equipment_id"])
    if p.get("operator_id"):
        qs = qs.filter(operator_id=p["operator_id"])
    if p.get("status"):
        qs = qs.filter(status__in=[x for x in p["status"].split(",") if x])
    if p.get("academic_year"):
        qs = qs.filter(academic_year=p["academic_year"])
    return Response({"results": [s.allocation_out(a, user, shifts=False) for a in qs.order_by("-created_at")[:MAX_LIST]]})


def _alloc_visible(alloc: DutyAllocation, user) -> bool:
    if alloc.operator_id == user.pk:
        return True
    ids = _manager_scope(user)
    return ids is None or alloc.equipment_id in ids or alloc.equipment_id in access.operator_equipment_ids(user)


@training_api(["GET"])
def duty_allocation_detail(request, pk: int):
    alloc = _get(DutyAllocation, pk)
    if not _alloc_visible(alloc, request.user):
        _forbid()
    return Response(s.allocation_out(alloc, request.user))


@training_api(["POST"])
def duty_allocation_action(request, pk: int, action: str):
    alloc = _get(DutyAllocation, pk)
    user = request.user
    reason = request.data.get("reason") or ""
    if action in ("confirm", "decline"):
        alloc = duty.respond(alloc, user, action=action, reason=reason, channel="PORTAL")
    elif action == "cancel":
        alloc = duty.cancel(alloc, user, reason=reason)
    elif action == "remind":
        alloc = duty.remind(alloc, user)
    else:
        raise TrainingError("Unknown action.", status=404)
    alloc.refresh_from_db()
    return Response(s.allocation_out(alloc, user))


def _public_allocation(alloc: DutyAllocation) -> dict:
    shifts = list(alloc.shifts.exclude(status=ShiftStatus.CANCELLED).order_by("start_at"))
    return {
        "reference": alloc.reference,
        "equipment": {"code": alloc.equipment.code, "name": alloc.equipment.name},
        "operator_name": get_user_display_name(alloc.operator),
        "status": alloc.status,
        "status_label": alloc.get_status_display(),
        "confirm_by": s.iso(alloc.confirm_by),
        "title": alloc.title,
        "note": alloc.note,
        "planned_minutes": alloc.planned_minutes,
        "shifts": [{"start_at": s.iso(x.start_at), "end_at": s.iso(x.end_at), "status": x.status} for x in shifts[:200]],
        "can_respond": alloc.status == DutyStatus.PENDING and (not alloc.confirm_by or alloc.confirm_by > timezone.now()),
    }


@public_api(["GET", "POST"])
def duty_respond(request):
    """Signed link from the duty email. GET only shows the duty; the page posts the operator's choice."""
    token = request.query_params.get("token") if request.method == "GET" else request.data.get("token")
    alloc = duty.from_token(token or "")
    if request.method == "POST":
        action = request.data.get("action")
        if action not in ("confirm", "decline"):
            raise TrainingError("action must be confirm or decline.")
        alloc = duty.respond(alloc, None, action=action, reason=request.data.get("reason") or "", channel="EMAIL")
    return Response(_public_allocation(alloc))


@training_api(["POST"])
def duty_shift_action(request, pk: int, action: str):
    shift = _get(DutyShift, pk)
    user = request.user
    if action == "check-in":
        duty.check_in(shift, user)
    elif action == "check-out":
        duty.check_out(shift, user)
    elif action == "verify":
        duty.verify(shift, user, minutes=request.data.get("operated_minutes"), remarks=request.data.get("remarks") or "")
    elif action == "missed":
        duty.verify(shift, user, minutes=0, remarks=request.data.get("remarks") or "", missed=True)
    else:
        raise TrainingError("Unknown action.", status=404)
    shift.refresh_from_db()
    return Response(s.shift_out(shift))


@training_api(["GET"])
def duty_calendar(request):
    """Calendar slots of one equipment with duty shifts overlaid, for the OIC's slot picker."""
    from iic_booking.equipment.models import DailySlot

    eq = _equipment(request.query_params.get("equipment_id"))
    if not access.can_view_equipment(request.user, eq.equipment_id):
        _forbid()
    today = timezone.localdate()
    d_from = parse_date(request.query_params.get("date_from") or "") or today
    d_to = parse_date(request.query_params.get("date_to") or "") or d_from + timedelta(days=6)
    if d_to < d_from or (d_to - d_from).days > 62:
        raise TrainingError("Choose up to 62 days.")
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(d_from, time.min), tz)
    end = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz)
    slots = DailySlot.objects.filter(
        slot_master__equipment_id=eq.equipment_id, start_datetime__gte=start, start_datetime__lt=end
    ).order_by("start_datetime")[:3000]
    shifts = (
        DutyShift.objects.filter(equipment=eq, start_at__lt=end, end_at__gt=start)
        .exclude(status__in=(ShiftStatus.CANCELLED, ShiftStatus.RELEASED))
        .select_related("operator", "allocation")
        .order_by("start_at")
    )
    return Response(
        {
            "equipment": s.equipment_brief(eq),
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "slots": [
                {
                    "id": x.id,
                    "start_at": s.iso(x.start_datetime),
                    "end_at": s.iso(x.end_datetime),
                    "status": x.status,
                    "booked": bool(x.booking_id),
                }
                for x in slots
            ],
            "shifts": [
                {
                    **s.shift_out(x),
                    "operator": s.user_brief(x.operator),
                    "allocation_status": x.allocation.status,
                    "reference": x.allocation.reference,
                }
                for x in shifts
            ],
        }
    )


@training_api(["GET"])
def duty_me(request):
    user = request.user
    now = timezone.now()
    qs = _alloc_qs().filter(operator=user)
    pending = qs.filter(status=DutyStatus.PENDING).order_by("confirm_by")
    active = qs.filter(status=DutyStatus.CONFIRMED).order_by("created_at")
    recent = qs.exclude(status__in=(DutyStatus.PENDING, DutyStatus.CONFIRMED)).order_by("-updated_at")[:30]
    upcoming = (
        DutyShift.objects.filter(operator=user, allocation__status=DutyStatus.CONFIRMED, status__in=duty.LIVE_SHIFT, end_at__gt=now)
        .select_related("equipment", "allocation")
        .order_by("start_at")[:50]
    )
    entries = OperatorRosterEntry.objects.filter(user=user).exclude(status=RosterStatus.REMOVED).select_related(
        "equipment", "user", "faculty", "department", "award", "award__level", "award__equipment", "award__user"
    )
    hours = accounting.summary({"operator_id": user.pk, "group_by": "equipment", "academic_year": request.query_params.get("academic_year") or ""}, None)
    return Response(
        {
            "pending": [s.allocation_out(a, user) for a in pending],
            "active": [s.allocation_out(a, user) for a in active],
            "recent": [s.allocation_out(a, user, shifts=False) for a in recent],
            "upcoming_shifts": [
                {
                    **s.shift_out(x),
                    "equipment": s.equipment_brief(x.equipment),
                    "reference": x.allocation.reference,
                    "can_check_in": x.status == ShiftStatus.SCHEDULED and x.start_at - duty.CHECKIN_EARLY <= now <= x.end_at,
                    "can_check_out": x.status == ShiftStatus.CHECKED_IN,
                }
                for x in upcoming
            ],
            "roster": [s.roster_out(e) for e in entries],
            "hours": hours,
        }
    )


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------
@training_api(["GET"])
def duty_accounting(request):
    ids = _manager_scope(request.user)
    _require_scope(ids)
    return Response(accounting.summary(request.query_params, ids))


@training_api(["GET"])
def duty_accounting_export(request):
    ids = _manager_scope(request.user)
    _require_scope(ids)
    body = accounting.export_csv(request.query_params, ids)
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="operator-duty-hours-{timezone.localdate():%Y%m%d}.csv"'
    return resp


@training_api(["GET"])
def duty_statement(request):
    user = request.user
    op_id = _int(request.query_params.get("operator_id")) or user.pk
    if op_id == user.pk:
        ids = None
    else:
        ids = _manager_scope(user)
        _require_scope(ids)
    data = accounting.statement(request.query_params, ids, operator_id=op_id)
    if request.query_params.get("format") == "csv":
        params = {**request.query_params.dict(), "operator_id": op_id}
        if not any(params.get(k) for k in ("month", "academic_year", "date_from", "date_to")):
            params["month"] = timezone.localdate().strftime("%Y-%m")
        resp = HttpResponse(accounting.export_csv(params, ids), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="duty-statement-{op_id}.csv"'
        return resp
    return Response(data)


@training_api(["GET"])
def duty_live(request):
    ids = _manager_scope(request.user)
    if ids is not None:
        ids = ids | access.operator_equipment_ids(request.user)
    _require_scope(ids)
    return Response(accounting.live(ids))


# ---------------------------------------------------------------------------
# Operator policy
# ---------------------------------------------------------------------------
def _policy_editor(user):
    if access.is_admin(user):
        return "all"
    return access.dept_admin_department_id(user)


@training_api(["GET", "POST"], allow_disabled=True)
def operator_policy_view(request):
    user = request.user
    editor = _policy_editor(user)
    oic_ids = access.oic_equipment_ids(user)
    if request.method == "POST":
        scope = request.data.get("scope") or PolicyScope.GLOBAL
        department = equipment = None
        if scope == PolicyScope.GLOBAL:
            if editor != "all":
                _forbid("Only the Main Admin can change the global operator policy.")
        elif scope == PolicyScope.DEPARTMENT:
            from iic_booking.users.models import Department

            department = _get(Department, request.data.get("department_id"))
            if editor != "all" and department.pk != editor:
                _forbid("You can only set the operator policy for your department.")
        elif scope == PolicyScope.EQUIPMENT:
            equipment = _equipment(request.data.get("equipment_id"))
            if editor != "all" and equipment.internal_department_id != editor and equipment.equipment_id not in oic_ids:
                _forbid("You can only set the operator policy for equipment you manage.")
        else:
            raise TrainingError("Invalid scope.")
        row = operator_policy.publish(
            scope=scope, department=department, equipment=equipment, data=operator_policy.clean(request.data), actor=user
        )
        audit(user, "operator_policy.published", row, after={"scope": scope, "version": row.version})
        return Response(operator_policy.out(row), status=status.HTTP_201_CREATED)
    if editor is None and not oic_ids:
        _forbid("Only administrators and OICs can view the operator policy.")
    qs = OperatorPolicy.objects.filter(is_active=True).select_related("department", "equipment", "created_by")
    if editor is None:
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(equipment_id__in=oic_ids))
    elif editor != "all":
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(department_id=editor) | Q(equipment__internal_department_id=editor) | Q(equipment_id__in=oic_ids))
    eq_id = _int(request.query_params.get("equipment_id"))
    effective = None
    if eq_id:
        effective = operator_policy.out(operator_policy.effective(_equipment(eq_id)))
    return Response(
        {
            "can_edit_global": editor == "all",
            "department_id": None if editor in (None, "all") else editor,
            "oic_equipment_ids": sorted(oic_ids),
            "defaults": operator_policy.out(OperatorPolicy(scope=PolicyScope.GLOBAL, version=0)),
            "policies": [operator_policy.out(p) for p in qs.order_by("scope", "id")],
            "effective": effective,
        }
    )


@training_api(["GET"], allow_disabled=True)
def operator_policy_history(request):
    user = request.user
    editor = _policy_editor(user)
    oic_ids = access.oic_equipment_ids(user)
    if editor is None and not oic_ids:
        _forbid()
    qs = OperatorPolicy.objects.select_related("department", "equipment", "created_by").order_by("-published_at", "-id")
    if editor is None:
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(equipment_id__in=oic_ids))
    elif editor != "all":
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(department_id=editor) | Q(equipment__internal_department_id=editor))
    return Response({"results": [operator_policy.out(p) for p in qs[:100]]})
