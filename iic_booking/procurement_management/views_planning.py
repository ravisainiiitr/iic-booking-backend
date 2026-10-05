"""Plan / non-plan requirements, consolidation and proposals."""

from __future__ import annotations

from django.db.models import Q
from rest_framework.response import Response

from . import access, planning
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_int, pm_api, req_reason
from .errors import not_found
from .models import PlanProposal, PlanRequirement

P = c.OfficePermission


def _req_qs():
    return PlanRequirement.objects.select_related("department", "laboratory", "equipment", "category", "raised_by").filter(is_archived=False)


def _get_req(request, pk) -> PlanRequirement:
    return access.get_visible(_req_qs(), request.pm_scope, pk, planning.requirements_q(request.pm_scope))


def _req_detail(r, status=200):
    return Response(s.requirement(_req_qs().get(pk=r.pk), detail=True), status=status)


@pm_api(["GET", "POST"])
def requirements(request):
    scope = request.pm_scope
    if request.method == "POST":
        return _req_detail(planning.create_requirement(scope, data_of(request), request=request), status=201)
    p = request.query_params
    qs = _req_qs().filter(planning.requirements_q(scope))
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    for f in ("financial_year", "funding_type"):
        if p.get(f):
            qs = qs.filter(**{f: p[f]})
    statuses = [x for x in (p.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if p.get("proposal_id"):
        qs = qs.filter(proposal_id=parse_int(p["proposal_id"], "proposal_id"))
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(Q(number__icontains=term) | Q(description__icontains=term))
    return paginate(request, qs.order_by("financial_year", "number"), s.requirement)


@pm_api(["GET", "PATCH"])
def requirement_detail(request, pk: int):
    r = _get_req(request, pk)
    if request.method == "PATCH":
        r = planning.update_draft(request.pm_scope, r, data_of(request), request=request)
    return _req_detail(r)


@pm_api(["POST"])
def requirement_action(request, pk: int, action: str):
    scope = request.pm_scope
    r = _get_req(request, pk)
    data = data_of(request)
    if action == "submit":
        planning.submit_requirement(scope, r, request=request)
    elif action == "office-edit":
        changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
        planning.office_edit(scope, r, changes, req_reason(data), request=request)
    elif action == "remove":
        planning.remove(scope, r, req_reason(data), request=request)
    elif action == "restore":
        planning.restore(scope, r, req_reason(data), request=request)
    elif action == "merge":
        changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
        planning.merge(scope, r, data.get("source_ids"), req_reason(data), changes, request=request)
    elif action == "split":
        planning.split(scope, r, data.get("parts"), req_reason(data), request=request)
    else:
        raise not_found("Unknown action.")
    return _req_detail(r)


def _proposal_q(scope) -> Q:
    return access.visible_department_wide_q(scope)


def _get_proposal(request, pk) -> PlanProposal:
    qs = PlanProposal.objects.select_related("department", "created_by").filter(is_archived=False)
    return access.get_visible(qs, request.pm_scope, pk, _proposal_q(request.pm_scope))


def _proposal_detail(p, status=200):
    return Response(s.proposal(PlanProposal.objects.select_related("department", "created_by").get(pk=p.pk), detail=True), status=status)


@pm_api(["GET", "POST"])
def proposals(request):
    scope = request.pm_scope
    if request.method == "POST":
        return _proposal_detail(planning.create_proposal(scope, data_of(request), request=request), status=201)
    qs = PlanProposal.objects.select_related("department", "created_by").filter(_proposal_q(scope), is_archived=False)
    p = request.query_params
    for f in ("financial_year", "funding_type", "status"):
        if p.get(f):
            qs = qs.filter(**{f: p[f]})
    return paginate(request, qs.order_by("-created_at"), s.proposal)


@pm_api(["GET"])
def proposal_detail(request, pk: int):
    return _proposal_detail(_get_proposal(request, pk))


@pm_api(["POST"])
def proposal_action(request, pk: int, action: str):
    scope = request.pm_scope
    p = _get_proposal(request, pk)
    if action == "requirements":
        planning.set_proposal_requirements(scope, p, data_of(request).get("requirement_ids") or [], request=request)
    elif action == "send":
        planning.send_proposal(scope, p, request=request)
    elif action == "decide":
        data = request.data
        payload = data_of(request) if not hasattr(data, "getlist") else {k: data.get(k) for k in data.keys()}
        if hasattr(data, "getlist") and data.get("amounts"):
            import json

            try:
                payload["amounts"] = json.loads(data.get("amounts"))
            except (TypeError, ValueError):
                payload["amounts"] = {}
        planning.decide_proposal(scope, p, payload, upload=request.FILES.get("file"), request=request)
    else:
        raise not_found("Unknown action.")
    return _proposal_detail(p)


@pm_api(["GET"])
def proposal_pdf(request, pk: int):
    from .exports import proposal_pdf_response

    p = _get_proposal(request, pk)
    return proposal_pdf_response(p)
