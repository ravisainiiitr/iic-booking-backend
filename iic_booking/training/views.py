"""Training & Certification API. Everything except the bootstrap and admin policy endpoints is off while
``TRAINING_MODULE_ENABLED`` is false."""

from __future__ import annotations

from datetime import timedelta
from functools import wraps

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from . import access, delivery, demo, selection
from . import serializers as s
from .errors import TrainingError
from .models import (
    AppealStatus,
    AwardStatus,
    CallStatus,
    CertificationAward,
    CertificationLevel,
    DemoRequest,
    DemoStatus,
    EventKind,
    NominationCall,
    NominationStatus,
    PolicyScope,
    RegistrationStatus,
    RunStatus,
    SelectionAppeal,
    SessionStatus,
    ShortlistEntry,
    ShortlistRun,
    TrainingEvent,
    TrainingNomination,
    TrainingPolicy,
    TrainingSession,
)
from .policy import effective_policy, publish_new_version

MAX_LIST = 300


def training_api(methods, *, allow_disabled: bool = False):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            if not allow_disabled and not access.module_enabled():
                return Response(
                    {"detail": "Training & Certification is not enabled.", "code": access.DISABLED_CODE},
                    status=status.HTTP_403_FORBIDDEN,
                )
            try:
                return fn(request, *args, **kwargs)
            except TrainingError as exc:
                return Response({"detail": exc.message, "code": exc.code, **exc.extra}, status=exc.status)

        return api_view(methods)(permission_classes([IsAuthenticated])(inner))

    return deco


def _get(model, pk, **filters):
    obj = model.objects.filter(pk=pk, **filters).first()
    if obj is None:
        raise TrainingError("Not found.", status=404, code="not_found")
    return obj


def _forbid(message="You do not have access to this."):
    raise TrainingError(message, status=403, code="forbidden")


def _staff_scope(user) -> set[int] | None:
    """Equipment the user can see in the training workspace (None = all)."""
    if access.is_admin(user):
        return None
    return access.oic_equipment_ids(user) | access.dept_admin_equipment_ids(user) | access.operator_equipment_ids(user)


def _scoped(qs, ids, field="equipment_id"):
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


# ---------------------------------------------------------------------------
# Bootstrap and lookups
# ---------------------------------------------------------------------------
@training_api(["GET"], allow_disabled=True)
def bootstrap(request):
    return Response(access.availability(request.user))


@training_api(["GET"])
def equipment_list(request):
    qs = access.pilot_equipment_queryset().select_related("internal_department").order_by("name")
    if request.query_params.get("managed") == "1":
        ids = access.managed_equipment_ids(request.user)
        qs = _scoped(qs, ids)
    search = (request.query_params.get("q") or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return Response({"results": [s.equipment_brief(e) for e in qs[:MAX_LIST]]})


@training_api(["GET"])
def equipment_detail(request, equipment_id: int):
    from iic_booking.equipment.models import Equipment

    eq = _get(Equipment, equipment_id)
    if not access.equipment_in_pilot(eq):
        raise TrainingError("Training is not enabled for this equipment.", code="not_in_pilot")
    p = effective_policy(eq)
    return Response(
        {
            **s.equipment_brief(eq),
            "demo_rate_per_hour": s.money(p.demo_rate_per_hour),
            "demo_max_minutes": p.demo_max_minutes,
            "demo_refund_full_days": p.demo_refund_full_days,
            "demo_refund_half_days": p.demo_refund_half_days,
            "can_manage": access.can_manage_equipment(request.user, eq.equipment_id),
        }
    )


@training_api(["GET"])
def free_windows(request, equipment_id: int):
    from iic_booking.equipment.models import Equipment

    from .slots import free_windows as _free

    eq = _get(Equipment, equipment_id)
    if not (access.is_faculty(request.user) or access.can_view_equipment(request.user, eq.equipment_id)):
        _forbid()
    today = timezone.localdate()
    d_from = parse_date(request.query_params.get("date_from") or "") or today
    d_to = parse_date(request.query_params.get("date_to") or "") or (d_from + timedelta(days=14))
    if d_to < d_from or (d_to - d_from).days > 62:
        raise TrainingError("Choose a date range of at most 62 days.")
    try:
        duration = int(request.query_params.get("duration") or 60)
    except ValueError:
        raise TrainingError("Duration must be in minutes.") from None
    return Response({"windows": _free(eq, date_from=d_from, date_to=d_to, duration_minutes=duration)})


# ---------------------------------------------------------------------------
# Demo requests
# ---------------------------------------------------------------------------
def _demo_for(request, pk) -> DemoRequest:
    req = _get(DemoRequest, pk)
    if req.requester_id != request.user.id and not access.can_view_equipment(request.user, req.equipment_id):
        _forbid()
    return req


@training_api(["GET", "POST"])
def demo_requests(request):
    user = request.user
    if request.method == "POST":
        req = demo.create_request(user, request.data)
        return Response(s.demo_out(req, user, detail=True), status=status.HTTP_201_CREATED)
    scope = request.query_params.get("scope") or ("inbox" if not access.is_faculty(user) else "mine")
    qs = DemoRequest.objects.select_related("equipment", "equipment__internal_department", "requester", "requester__department", "decided_by")
    if scope == "mine":
        qs = qs.filter(requester=user)
    else:
        ids = _staff_scope(user)
        if ids is not None and not ids:
            _forbid()
        qs = _scoped(qs, ids)
    st = request.query_params.get("status")
    if st == "open":
        qs = qs.filter(status__in=demo.INBOX_STATUSES + (DemoStatus.SCHEDULED,))
    elif st:
        qs = qs.filter(status__in=st.split(","))
    return Response({"results": [s.demo_out(r, user) for r in qs[:MAX_LIST]]})


@training_api(["GET"])
def demo_request_detail(request, pk: int):
    req = _demo_for(request, pk)
    demo.mark_viewed(req, request.user)
    req.refresh_from_db()
    return Response(s.demo_out(req, request.user, detail=True))


@training_api(["POST"])
def demo_request_action(request, pk: int, action: str):
    req = _demo_for(request, pk)
    user, d = request.user, request.data
    if action == "decide":
        req = demo.decide(req, user, d)
    elif action == "respond":
        req = demo.respond(req, user, response=d.get("response"), windows=d.get("windows"), note=d.get("note") or "")
    elif action == "schedule":
        req = demo.schedule(req, user, d.get("start_at"))
    elif action == "cancel":
        req = demo.cancel(req, user, d.get("reason") or "")
    elif action == "withdraw":
        req = demo.withdraw(req, user, d.get("reason") or "")
    elif action == "attendance":
        req = demo.record_attendance(req, user, attended_count=d.get("attended_count"), present_user_ids=d.get("present_user_ids"))
    elif action == "complete":
        req = demo.complete(req, user, no_show=bool(d.get("no_show")), attended_count=d.get("attended_count"))
    else:
        raise TrainingError("Unknown action.", status=404)
    req.refresh_from_db()
    return Response(s.demo_out(req, user, detail=True))


# ---------------------------------------------------------------------------
# Calls, nominations, shortlists, appeals
# ---------------------------------------------------------------------------
def _call_visible(call: NominationCall, user) -> bool:
    if access.can_view_equipment(user, call.equipment_id) or access.is_faculty(user):
        return True
    return call.nominations.filter(student=user).exists()


@training_api(["GET", "POST"])
def calls(request):
    user = request.user
    if request.method == "POST":
        call = selection.open_call(user, request.data)
        return Response(s.call_out(call, user), status=status.HTTP_201_CREATED)
    qs = NominationCall.objects.select_related("equipment", "equipment__internal_department")
    scope = request.query_params.get("scope") or ("manage" if not access.is_faculty(user) else "open")
    if scope == "manage":
        ids = _staff_scope(user)
        if ids is not None and not ids:
            _forbid()
        qs = _scoped(qs, ids)
    elif scope == "open":
        if not access.is_faculty(user):
            _forbid()
        qs = qs.filter(status=CallStatus.OPEN, deadline__gt=timezone.now())
    elif scope == "mine":
        qs = qs.filter(Q(nominations__nominator=user) | Q(nominations__student=user)).distinct()
    st = request.query_params.get("status")
    if st:
        qs = qs.filter(status__in=st.split(","))
    return Response({"results": [s.call_out(c, user) for c in qs[:MAX_LIST]]})


@training_api(["GET"])
def call_detail(request, pk: int):
    call = _get(NominationCall, pk)
    if not _call_visible(call, request.user):
        _forbid()
    data = s.call_out(call, request.user)
    data["event"] = s.event_out(call.event)
    return Response(data)


@training_api(["POST"])
def call_close(request, pk: int):
    call = selection.close_call(_get(NominationCall, pk), request.user)
    return Response(s.call_out(call, request.user))


@training_api(["GET"])
def call_nominations(request, pk: int):
    call = _get(NominationCall, pk)
    user = request.user
    qs = call.nominations.select_related("student", "student__department", "nominator", "call", "call__equipment")
    if not access.can_view_equipment(user, call.equipment_id):
        if not access.is_faculty(user):
            _forbid()
        qs = qs.filter(nominator=user)
    return Response({"results": [s.nomination_out(n, user) for n in qs]})


@training_api(["GET", "POST"])
def call_shortlist(request, pk: int):
    call = _get(NominationCall, pk)
    user = request.user
    if request.method == "POST":
        run = selection.run_preview(call, user, public_input=request.data.get("public_input") or "")
        return Response(s.run_out(run), status=status.HTTP_201_CREATED)
    if not access.can_view_equipment(user, call.equipment_id):
        _forbid()
    run = call.shortlist_runs.filter(status__in=(RunStatus.PUBLISHED, RunStatus.DRAFT)).order_by("-run_at", "-id").first()
    published = call.shortlist_runs.filter(status=RunStatus.PUBLISHED).first()
    run = published or run
    return Response({"run": s.run_out(run) if run else None})


@training_api(["GET"])
def call_results(request, pk: int):
    call = _get(NominationCall, pk)
    if not _call_visible(call, request.user):
        _forbid()
    run = selection.published_run(call)
    if run is None:
        raise TrainingError("The selection has not been published yet.", status=404, code="not_published")
    return Response(s.run_public(run))


@training_api(["GET", "POST"])
def nominations(request):
    user = request.user
    if request.method == "POST":
        n = selection.nominate(user, request.data)
        return Response(s.nomination_out(n, user), status=status.HTTP_201_CREATED)
    qs = TrainingNomination.objects.select_related("call", "call__equipment", "student", "student__department", "nominator")
    qs = qs.filter(Q(nominator=user) | Q(student=user))
    if request.query_params.get("active") == "1":
        qs = qs.exclude(status__in=(NominationStatus.WITHDRAWN,))
    return Response({"results": [s.nomination_out(n, user) for n in qs[:MAX_LIST]]})


@training_api(["POST"])
def nomination_action(request, pk: int, action: str):
    n = _get(TrainingNomination, pk)
    user, d = request.user, request.data
    if action == "withdraw":
        n = selection.withdraw_nomination(n, user)
    elif action == "confirm-interest":
        n = selection.confirm_interest(n, user, sop_acknowledged=bool(d.get("sop_acknowledged")))
    elif action == "accept-seat":
        n = selection.accept_seat(n, user, sop_acknowledged=bool(d.get("sop_acknowledged")))
    elif action == "decline-seat":
        n = selection.decline_seat(n, user)
    elif action == "adjust-need":
        n = selection.adjust_need(n, user, points=d.get("points"), reason=d.get("reason") or "")
    else:
        raise TrainingError("Unknown action.", status=404)
    n.refresh_from_db()
    return Response(s.nomination_out(n, user))


@training_api(["POST"])
def run_publish(request, pk: int):
    run = selection.publish(_get(ShortlistRun, pk), request.user, public_input=request.data.get("public_input") or "")
    return Response(s.run_out(run))


@training_api(["GET"])
def run_export(request, pk: int):
    run = _get(ShortlistRun, pk)
    if not access.can_view_equipment(request.user, run.call.equipment_id):
        _forbid()
    resp = HttpResponse(selection.export_csv(run), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="shortlist-{run.call.reference}-run{run.pk}.csv"'
    return resp


@training_api(["GET"])
def run_verify(request, pk: int):
    run = _get(ShortlistRun, pk)
    if not _call_visible(run.call, request.user):
        _forbid()
    return Response(selection.verify_run(run))


@training_api(["POST"])
def entry_override(request, pk: int):
    entry = _get(ShortlistEntry, pk)
    run = selection.override_entry(entry, request.user, outcome=request.data.get("outcome"), reason=request.data.get("reason"))
    return Response(s.run_out(run))


@training_api(["POST"])
def entry_appeal(request, pk: int):
    appeal = selection.submit_appeal(_get(ShortlistEntry, pk), request.user, request.data.get("reason") or "")
    return Response({"id": appeal.pk, "status": appeal.status}, status=status.HTTP_201_CREATED)


def _appeal_out(a: SelectionAppeal, viewer) -> dict:
    e = a.entry
    call = e.run.call
    return {
        "id": a.pk,
        "status": a.status,
        "reason": a.reason,
        "decision_note": a.decision_note,
        "created_at": s.iso(a.created_at),
        "decided_at": s.iso(a.decided_at),
        "submitted_by": s.user_brief(a.submitted_by),
        "decided_by": s.user_brief(a.decided_by) if a.decided_by_id else None,
        "call": {"id": call.pk, "reference": call.reference, "title": call.title, "equipment": s.equipment_brief(call.equipment)},
        "student": s.user_brief(e.nomination.student),
        "entry": s.entry_public(e),
        "can_decide": a.status == AppealStatus.PENDING and access.can_decide_appeal(viewer, call.equipment_id),
    }


@training_api(["GET"])
def appeals(request):
    user = request.user
    ids = None if access.is_admin(user) else access.oic_equipment_ids(user) | access.dept_admin_equipment_ids(user)
    if ids is not None and not ids:
        _forbid()
    qs = _scoped(
        SelectionAppeal.objects.select_related("entry__run__call__equipment", "entry__nomination__student", "submitted_by", "decided_by"),
        ids,
        "entry__run__call__equipment_id",
    )
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    return Response({"results": [_appeal_out(a, user) for a in qs[:MAX_LIST]]})


@training_api(["POST"])
def appeal_decide(request, pk: int):
    a = selection.decide_appeal(
        _get(SelectionAppeal, pk), request.user, decision=request.data.get("decision"), note=request.data.get("note") or ""
    )
    return Response(_appeal_out(a, request.user))


# ---------------------------------------------------------------------------
# Events, sessions, attendance
# ---------------------------------------------------------------------------
def _event_visible(event: TrainingEvent, user) -> bool:
    if event.equipment_id and access.can_view_equipment(user, event.equipment_id):
        return True
    return event.registrations.filter(user=user).exists()


@training_api(["GET"])
def events(request):
    ids = _staff_scope(request.user)
    if ids is not None and not ids:
        _forbid()
    qs = _scoped(TrainingEvent.objects.select_related("equipment", "level_awarded"), ids)
    if request.query_params.get("include_demos") != "1":
        qs = qs.exclude(kind=EventKind.DEMO)
    st = request.query_params.get("status")
    if st:
        qs = qs.filter(status__in=st.split(","))
    return Response({"results": [s.event_out(e) for e in qs[:MAX_LIST]]})


@training_api(["GET", "PATCH"])
def event_detail(request, pk: int):
    event = _get(TrainingEvent, pk)
    if request.method == "PATCH":
        event = delivery.update_event(event, request.user, request.data)
    elif not _event_visible(event, request.user):
        _forbid()
    return Response(s.event_out(event))


@training_api(["POST"])
def event_cancel(request, pk: int):
    event = delivery.cancel_event(_get(TrainingEvent, pk), request.user, reason=request.data.get("reason") or "")
    return Response(s.event_out(event))


@training_api(["POST"])
def event_sessions(request, pk: int):
    session = delivery.add_session(_get(TrainingEvent, pk), request.user, request.data)
    return Response(s.session_out(session), status=status.HTTP_201_CREATED)


@training_api(["PATCH", "DELETE"])
def session_detail(request, pk: int):
    session = _get(TrainingSession, pk)
    if request.method == "DELETE":
        session = delivery.cancel_session(session, request.user, note=request.data.get("note") or "")
    else:
        session = delivery.update_session(session, request.user, request.data)
    return Response(s.session_out(session))


@training_api(["POST"])
def session_reserve(request, pk: int):
    return Response(delivery.reserve(_get(TrainingSession, pk), request.user))


@training_api(["POST"])
def session_release(request, pk: int):
    return Response(delivery.release(_get(TrainingSession, pk), request.user, note=request.data.get("note") or ""))


@training_api(["GET", "POST"])
def session_attendance(request, pk: int):
    session = _get(TrainingSession, pk)
    equipment_id = session.equipment_id or session.event.equipment_id
    if not access.can_mark_attendance(request.user, equipment_id):
        _forbid("Only the OIC or a Lab Operator of this equipment can view or mark attendance.")
    if request.method == "POST":
        out = delivery.mark_attendance(session, request.user, request.data.get("rows") or [])
        session.refresh_from_db()
        return Response({**out, "session": s.session_out(session), "roster": delivery.roster(session)})
    return Response({"session": s.session_out(session), "event": s.event_out(session.event, with_sessions=False), "roster": delivery.roster(session)})


@training_api(["GET"])
def attendance_sessions(request):
    user = request.user
    ids = None if access.is_admin(user) else access.oic_equipment_ids(user) | access.operator_equipment_ids(user)
    if ids is not None and not ids:
        _forbid()
    now = timezone.now()
    qs = TrainingSession.objects.select_related("event", "event__equipment").exclude(status=SessionStatus.CANCELLED)
    qs = qs.filter(start_at__gte=now - timedelta(days=30), start_at__lte=now + timedelta(days=30))
    if ids is not None:
        qs = qs.filter(Q(equipment_id__in=ids) | Q(equipment__isnull=True, event__equipment_id__in=ids))
    rows = []
    for sess in qs.order_by("start_at")[:MAX_LIST]:
        row = s.session_out(sess)
        row["event"] = {"id": sess.event_id, "title": sess.event.title, "kind": sess.event.kind, "equipment": s.equipment_brief(sess.event.equipment)}
        row["needs_attendance"] = sess.status != SessionStatus.COMPLETED and sess.end_at <= now
        row["demo_request_id"] = sess.event.demo_requests.values_list("id", flat=True).first() if sess.event.kind == EventKind.DEMO else None
        rows.append(row)
    return Response({"results": rows})


# ---------------------------------------------------------------------------
# Certifications, badges, personal views
# ---------------------------------------------------------------------------
@training_api(["GET"])
def certifications(request):
    ids = _staff_scope(request.user)
    if ids is not None and not ids:
        _forbid()
    qs = _scoped(CertificationAward.objects.select_related("user", "user__department", "equipment", "level"), ids)
    if request.query_params.get("equipment_id"):
        qs = qs.filter(equipment_id=request.query_params["equipment_id"])
    return Response({"results": [s.award_out(a) for a in qs[:MAX_LIST]]})


def _badge_allowed_ids(user, requested: list[int]) -> list[int]:
    if access.is_admin(user) or user.user_type in (UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN):
        return requested
    allowed = {user.id}
    if access.is_faculty(user):
        allowed |= access.faculty_group_student_ids(user)
    return [i for i in requested if i in allowed]


@training_api(["GET"], allow_disabled=True)
def badges(request):
    if not access.module_enabled():
        return Response({"results": {}})
    raw = (request.query_params.get("user_ids") or str(request.user.id)).split(",")
    requested = [int(x) for x in raw if x.strip().isdigit()][:500]
    ids = _badge_allowed_ids(request.user, requested)
    return Response({"results": {str(k): v for k, v in delivery.badges_for_users(ids).items()}})


@training_api(["GET"])
def my_trainings(request):
    user = request.user
    noms = TrainingNomination.objects.filter(student=user).select_related("call", "call__equipment", "student", "nominator")
    regs = user.training_registrations.select_related("event", "event__equipment").exclude(status=RegistrationStatus.CANCELLED)
    awards = CertificationAward.objects.filter(user=user).select_related("user", "equipment", "level")
    return Response(
        {
            "nominations": [s.nomination_out(n, user) for n in noms[:MAX_LIST]],
            "events": [{**s.event_out(r.event), "registration_status": r.status} for r in regs[:MAX_LIST]],
            "certifications": [s.award_out(a) for a in awards],
            "badges": delivery.badges_for_users([user.id]).get(user.id, []),
        }
    )


@training_api(["GET"])
def faculty_students_trainings(request):
    user = request.user
    if not access.is_faculty(user):
        _forbid()
    from iic_booking.users.models import User

    ids = access.faculty_group_student_ids(user)
    students = User.objects.filter(id__in=ids).select_related("department").order_by("name")
    awards: dict[int, list] = {}
    for a in CertificationAward.objects.filter(user_id__in=ids).select_related("user", "equipment", "level"):
        awards.setdefault(a.user_id, []).append(s.award_out(a))
    noms: dict[int, list] = {}
    for n in TrainingNomination.objects.filter(student_id__in=ids).select_related("call", "call__equipment"):
        noms.setdefault(n.student_id, []).append(
            {"id": n.pk, "call_title": n.call.title, "equipment": s.equipment_brief(n.call.equipment), "status": n.status, "status_label": n.get_status_display(), "mine": n.nominator_id == user.id}
        )
    badge_map = delivery.badges_for_users(ids)
    return Response(
        {
            "results": [
                {
                    "student": s.user_brief(st),
                    "certifications": awards.get(st.id, []),
                    "nominations": noms.get(st.id, []),
                    "badges": badge_map.get(st.id, []),
                }
                for st in students
            ]
        }
    )


@training_api(["GET"])
def workspace_summary(request):
    user = request.user
    ids = _staff_scope(user)
    if ids is not None and not ids:
        _forbid()
    oic_ids = None if access.is_admin(user) else access.oic_equipment_ids(user)
    now = timezone.now()
    return Response(
        {
            "roles": access.availability(user)["roles"],
            "demo_open": _scoped(DemoRequest.objects.filter(status__in=(DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW)), ids).count(),
            "demo_to_schedule": _scoped(DemoRequest.objects.filter(status=DemoStatus.APPROVED), ids).count(),
            "calls_open": _scoped(NominationCall.objects.filter(status=CallStatus.OPEN), ids).count(),
            "calls_to_publish": _scoped(
                NominationCall.objects.filter(Q(status=CallStatus.CLOSED) | Q(status=CallStatus.OPEN, deadline__lte=now)), ids
            ).count(),
            "appeals_pending": _scoped(
                SelectionAppeal.objects.filter(status=AppealStatus.PENDING), ids, "entry__run__call__equipment_id"
            ).count(),
            "attendance_due": _scoped(
                TrainingSession.objects.filter(status__in=(SessionStatus.PLANNED, SessionStatus.SCHEDULED), end_at__lte=now).exclude(event__kind=EventKind.DEMO),
                ids,
                "event__equipment_id",
            ).count(),
            "certified_active": _scoped(CertificationAward.objects.filter(status=AwardStatus.ACTIVE), ids).count(),
            "can_manage": access.is_admin(user) or bool(oic_ids),
        }
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def _policy_editor_scope(user):
    """'all' for Main Admin, a department id for dept admins with training.manage, else None."""
    if access.is_admin(user):
        return "all"
    return access.dept_admin_department_id(user)


INT_FIELDS = {
    "per_faculty_cap": (1, 10),
    "per_department_pct": (5, 100),
    "reserved_pct": (0, 60),
    "cooldown_months": (0, 36),
    "min_tenure_months_after_training": (0, 24),
    "suspension_lookback_months": (0, 60),
    "trained_validity_months": (1, 120),
    "dormancy_months": (1, 60),
    "seat_confirm_hours": (12, 240),
    "appeal_working_days": (1, 15),
    "proposal_expiry_working_days": (1, 15),
    "review_sla_working_days": (1, 30),
    "demo_max_minutes": (15, 720),
    "demo_refund_full_days": (0, 60),
    "demo_refund_half_days": (0, 60),
}


def _clean_policy_data(raw: dict) -> dict:
    from decimal import Decimal, InvalidOperation

    from .models import DEFAULT_SCORING_WEIGHTS

    out = {}
    for f, (lo, hi) in INT_FIELDS.items():
        if f in raw and raw[f] not in (None, ""):
            try:
                v = int(raw[f])
            except (TypeError, ValueError):
                raise TrainingError(f"{f} must be a whole number.") from None
            if not lo <= v <= hi:
                raise TrainingError(f"{f} must be between {lo} and {hi}.")
            out[f] = v
    if "demo_rate_per_hour" in raw and raw["demo_rate_per_hour"] not in (None, ""):
        try:
            rate = Decimal(str(raw["demo_rate_per_hour"]))
        except InvalidOperation:
            raise TrainingError("demo_rate_per_hour must be a number.") from None
        if rate < 0 or rate > 100000:
            raise TrainingError("demo_rate_per_hour must be between 0 and 100000.")
        out["demo_rate_per_hour"] = rate.quantize(Decimal("0.01"))
    if "underrepresented_override_department_ids" in raw:
        ids = raw.get("underrepresented_override_department_ids") or []
        if not isinstance(ids, list):
            raise TrainingError("underrepresented_override_department_ids must be a list.")
        out["underrepresented_override_department_ids"] = [int(x) for x in ids if str(x).isdigit()]
    if "scoring_weights" in raw:
        weights = raw.get("scoring_weights") or {}
        if not isinstance(weights, dict):
            raise TrainingError("scoring_weights must be an object.")
        clean = {}
        for k, v in weights.items():
            if k not in DEFAULT_SCORING_WEIGHTS:
                continue
            try:
                clean[k] = float(v)
            except (TypeError, ValueError):
                raise TrainingError(f"Weight {k} must be a number.") from None
        out["scoring_weights"] = clean
    if "notes" in raw:
        out["notes"] = (raw.get("notes") or "").strip()
    return out


@training_api(["GET", "POST"], allow_disabled=True)
def policy(request):
    user = request.user
    editor = _policy_editor_scope(user)
    if editor is None:
        _forbid("Only administrators can view or change the training policy.")
    if request.method == "POST":
        scope = request.data.get("scope") or PolicyScope.GLOBAL
        department = equipment = None
        if scope == PolicyScope.GLOBAL:
            if editor != "all":
                _forbid("Only the Main Admin can change the global policy.")
        elif scope == PolicyScope.DEPARTMENT:
            from iic_booking.users.models import Department

            department = _get(Department, request.data.get("department_id"))
            if editor != "all" and department.pk != editor:
                _forbid("You can only set the policy for your department.")
        elif scope == PolicyScope.EQUIPMENT:
            from iic_booking.equipment.models import Equipment

            equipment = _get(Equipment, request.data.get("equipment_id"))
            if editor != "all" and equipment.internal_department_id != editor:
                _forbid("You can only set the policy for your department's equipment.")
        else:
            raise TrainingError("Invalid scope.")
        row = publish_new_version(scope=scope, department=department, equipment=equipment, data=_clean_policy_data(request.data), actor=user)
        from .audit import audit

        audit(user, "policy.published", row, after={"scope": scope, "version": row.version})
        return Response(s.policy_out(row), status=status.HTTP_201_CREATED)
    qs = TrainingPolicy.objects.filter(is_active=True).select_related("department", "equipment", "created_by")
    if editor != "all":
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(department_id=editor) | Q(equipment__internal_department_id=editor))
    from .models import DEFAULT_SCORING_WEIGHTS

    return Response(
        {
            "module_enabled": access.module_enabled(),
            "pilot_equipment_codes": sorted(access.pilot_equipment_codes()),
            "pilot_oic_count": len(access.pilot_oic_emails()),
            "can_edit_global": editor == "all",
            "department_id": None if editor == "all" else editor,
            "default_weights": DEFAULT_SCORING_WEIGHTS,
            "policies": [s.policy_out(p) for p in qs.order_by("scope", "id")],
            "levels": [
                {"code": lv.code, "name": lv.name, "rank": lv.rank, "is_active": lv.is_active, "default_validity_months": lv.default_validity_months}
                for lv in CertificationLevel.objects.order_by("rank")
            ],
        }
    )


@training_api(["GET"], allow_disabled=True)
def policy_history(request):
    editor = _policy_editor_scope(request.user)
    if editor is None:
        _forbid()
    qs = TrainingPolicy.objects.select_related("department", "equipment", "created_by").order_by("-published_at", "-id")
    if editor != "all":
        qs = qs.filter(Q(scope=PolicyScope.GLOBAL) | Q(department_id=editor) | Q(equipment__internal_department_id=editor))
    return Response({"results": [s.policy_out(p) for p in qs[:100]]})
