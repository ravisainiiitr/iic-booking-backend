"""Training API: authorisation to book (gate status, training requests, authorised users, grandfathering)."""

from __future__ import annotations

from django.db.models import Q
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response

from . import access, authorisation
from . import serializers as s
from .models import OPEN_REQUEST_STATUSES, AuthorisationRequest, CertificationAward
from .views import MAX_LIST, _forbid, _get, training_api


def _equipment(pk):
    from iic_booking.equipment.models import Equipment

    return _get(Equipment, pk)


def _ids(raw) -> list[int]:
    return [int(x) for x in str(raw or "").split(",") if x.strip().isdigit()][:200]


@training_api(["GET"], allow_disabled=True)
def gated_equipment(request):
    """Equipment where booking needs authorisation (for "Training required" badges). Empty while the module is off."""
    return Response({"equipment_ids": sorted(authorisation.gated_equipment_ids())})


@training_api(["GET"], allow_disabled=True)
def status_view(request):
    """The signed-in user's authorisation status for ``equipment_ids`` (or ``user_id`` for staff booking on behalf)."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models import User

    ids = _ids(request.query_params.get("equipment_ids"))
    subject = request.user
    uid = request.query_params.get("user_id")
    if uid and str(uid).isdigit() and int(uid) != request.user.pk:
        if not authorisation.may_bypass(request.user):
            _forbid()
        subject = _get(User, int(uid))
    eqs = list(Equipment.objects.filter(equipment_id__in=ids))
    results = authorisation.statuses_for(subject, eqs)
    if subject.pk != request.user.pk:
        for v in results.values():
            v["staff_bypass"] = not v["allowed"]
    return Response({"results": {str(k): v for k, v in results.items()}})


@training_api(["GET"], allow_disabled=True)
def my_authorisations(request):
    if not access.module_enabled() or not access.in_audience(request.user):
        return Response({"equipment": [], "certifications": [], "requests": [], "supervisor": None})
    out = authorisation.my_overview(request.user)
    out["supervisor"] = authorisation.supervisor_overview(request.user) if access.is_faculty(request.user) else None
    return Response(out)


@training_api(["GET", "POST"])
def requests_view(request):
    user = request.user
    if request.method == "POST":
        eq = _equipment(request.data.get("equipment_id"))
        req = authorisation.create_request(user, eq, str(request.data.get("message") or ""))
        return Response(authorisation.request_out(req, user), status=status.HTTP_201_CREATED)
    qs = AuthorisationRequest.objects.select_related("user", "user__department", "user__supervisor", "equipment",
                                                     "equipment__internal_department", "decided_by")
    p = request.query_params
    if p.get("scope") == "inbox":
        ids = authorisation.handled_equipment_ids(user)
        if ids is not None:
            if not ids:
                _forbid("Only the OIC or a Lab Operator can see training requests.")
            qs = qs.filter(equipment_id__in=ids)
    else:
        qs = qs.filter(user=user)
    if p.get("equipment_id"):
        qs = qs.filter(equipment_id=p["equipment_id"])
    if p.get("status") == "open":
        qs = qs.filter(status__in=OPEN_REQUEST_STATUSES)
    elif p.get("status"):
        qs = qs.filter(status=p["status"])
    return Response({"results": [authorisation.request_out(r, user) for r in qs[:MAX_LIST]]})


@training_api(["POST"])
def request_action(request, pk: int, action: str):
    req = _get(AuthorisationRequest, pk)
    req = authorisation.act_on_request(req, request.user, action, request.data)
    req.refresh_from_db()
    return Response(authorisation.request_out(req, request.user))


@training_api(["GET"])
def managed_equipment(request):
    """Equipment the user handles, with gate settings and counts (OIC / Lab Operator / Main Admin)."""
    ids = authorisation.handled_equipment_ids(request.user)
    if ids is not None and not ids:
        _forbid("Only the OIC or a Lab Operator can open this.")
    qs = access.pilot_equipment_queryset().select_related("internal_department").order_by("name")
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
    eqs = list(qs[:MAX_LIST])
    open_counts: dict[int, int] = {}
    for eid in AuthorisationRequest.objects.filter(equipment__in=eqs, status__in=OPEN_REQUEST_STATUSES).values_list("equipment_id", flat=True):
        open_counts[eid] = open_counts.get(eid, 0) + 1
    rows = []
    for eq in eqs:
        st = authorisation.setting_out(eq)
        rows.append({**st, "open_requests": open_counts.get(eq.equipment_id, 0),
                     "can_configure": authorisation.can_configure(request.user, eq.equipment_id)})
    return Response({"results": rows, "total": len(rows)})


def _require_handle(user, eq):
    if not authorisation.can_handle(user, eq.equipment_id):
        _forbid("Only the OIC or a Lab Operator of this equipment can open this.")


@training_api(["GET", "PUT"])
def equipment_setting(request, equipment_id: int):
    eq = _equipment(equipment_id)
    if request.method == "PUT":
        return Response(authorisation.save_setting(eq, request.user, request.data))
    _require_handle(request.user, eq)
    return Response({**authorisation.setting_out(eq), "can_configure": authorisation.can_configure(request.user, eq.equipment_id)})


@training_api(["GET", "POST"])
def equipment_users(request, equipment_id: int):
    eq = _equipment(equipment_id)
    if request.method == "POST":
        out = authorisation.bulk_grant(eq, request.user, request.data.get("user_ids") or [], months=request.data.get("months"),
                                       reason=str(request.data.get("reason") or ""))
        return Response(out, status=status.HTTP_201_CREATED)
    _require_handle(request.user, eq)
    rows = authorisation.authorised_users(eq, include_closed=request.query_params.get("include_closed") == "1")
    return Response({"results": rows})


@training_api(["GET"])
def equipment_users_export(request, equipment_id: int):
    eq = _equipment(equipment_id)
    _require_handle(request.user, eq)
    resp = HttpResponse(authorisation.export_csv(eq), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="authorised-users-{eq.code}.csv"'
    return resp


@training_api(["GET"])
def equipment_people(request, equipment_id: int):
    """People an OIC / Lab Operator can authorise (search by name or email)."""
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    eq = _equipment(equipment_id)
    _require_handle(request.user, eq)
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": []})
    qs = (
        User.objects.filter(is_active=True)
        .exclude(user_type__in=list(UserType.get_management_user_codes()))
        .filter(Q(name__icontains=q) | Q(email__icontains=q))
        .select_related("department")
        .order_by("name")[:20]
    )
    return Response({"results": [s.user_brief(u) for u in qs]})


@training_api(["GET", "POST"])
def equipment_grandfather(request, equipment_id: int):
    eq = _equipment(equipment_id)
    data = request.data if request.method == "POST" else request.query_params
    return Response(authorisation.grandfather(eq, request.user, data, apply=request.method == "POST"))


@training_api(["POST"])
def award_action(request, pk: int, action: str):
    award = _get(CertificationAward, pk)
    award = authorisation.act_on_award(award, request.user, action, request.data)
    return Response(s.award_out(award))
