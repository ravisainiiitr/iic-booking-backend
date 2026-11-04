"""Research impact API (``/api/v1/impact/``): funding lines, acknowledgement wording and check, ORCID and
publication suggestions, and the funder impact report (JSON, CSV, Excel)."""

from __future__ import annotations

import csv
import io
from functools import wraps

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import service as svc
from .models import EquipmentFunding, PublicationSuggestion, ResearcherOrcid, SuggestionStatus


def impact_api(methods):
    def deco(fn):
        @api_view(methods)
        @permission_classes([IsAuthenticated])
        @wraps(fn)
        def inner(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except svc.ImpactError as exc:
                return Response({"detail": exc.message, "code": exc.code}, status=exc.status)

        return inner

    return deco


def funding_out(f: EquipmentFunding) -> dict:
    return {
        "id": f.pk,
        "equipment": {"equipment_id": f.equipment_id, "name": f.equipment.name, "code": f.equipment.code},
        "funder": f.funder,
        "scheme": f.scheme,
        "sanction_reference": f.sanction_reference,
        "sanction_date": f.sanction_date.isoformat() if f.sanction_date else None,
        "amount": float(f.amount) if f.amount is not None else None,
        "acknowledgement_phrase": f.acknowledgement_phrase,
        "notes": f.notes,
        "is_active": f.is_active,
    }


def suggestion_out(s: PublicationSuggestion) -> dict:
    return {
        "id": s.pk,
        "doi": s.doi,
        "title": s.title,
        "journal": s.journal,
        "year": s.year,
        "authors": s.authors,
        "equipment_ids": s.equipment_ids,
        "status": s.status,
        "status_label": s.get_status_display(),
        "source": s.source,
    }


def policy_out() -> dict:
    p = svc.policy()
    return {
        "wording": p.wording if p else "",
        "required_phrases": p.required_phrases if p else [],
        "include_equipment_name": bool(p and p.include_equipment_name),
        "updated_at": p.updated_at.isoformat() if p else None,
    }


# --------------------------------------------------------------------------- funding
@impact_api(["GET", "POST"])
def funding_list(request):
    if request.method == "POST":
        from iic_booking.equipment.models import Equipment

        eq = get_object_or_404(Equipment, pk=request.data.get("equipment_id") or 0)
        return Response(funding_out(svc.add_funding(eq, request.user, request.data)), status=201)
    ids = svc.report_equipment_ids(request.user)
    svc.require(ids is None or bool(ids), "Funding lines are for the Main Admin, OICs and Department Admins.")
    qs = EquipmentFunding.objects.select_related("equipment")
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    if request.query_params.get("equipment_id"):
        qs = qs.filter(equipment_id=request.query_params["equipment_id"])
    if request.query_params.get("include_closed") not in ("1", "true"):
        qs = qs.filter(is_active=True)
    funders = sorted({(f.funder, f.scheme) for f in qs})
    return Response({
        "results": [funding_out(f) for f in qs[:1000]],
        "funders": [{"funder": a, "scheme": b} for a, b in funders],
    })


@impact_api(["PATCH"])
def funding_detail(request, pk: int):
    row = get_object_or_404(EquipmentFunding.objects.select_related("equipment"), pk=pk)
    return Response(funding_out(svc.update_funding(row, request.user, request.data)))


# --------------------------------------------------------------------------- acknowledgement
@impact_api(["GET", "PUT"])
def acknowledgement_policy(request):
    if request.method == "PUT":
        svc.save_policy(request.user, request.data)
    return Response(policy_out())


@impact_api(["POST"])
def acknowledgement_check(request):
    """Stateless check (any signed-in user, e.g. before submitting a claim); with ``claim_id`` the result is stored."""
    text = str(request.data.get("text") or "")
    if request.data.get("claim_id"):
        from iic_booking.equipment.models import EquipmentPublicationClaim

        claim = get_object_or_404(EquipmentPublicationClaim, pk=request.data["claim_id"])
        row = svc.store_claim_check(claim, request.user, text)
        return Response({"result": row.result, "found": row.found, "missing": row.missing, "stored": True,
                         "wording": policy_out()["wording"]})
    ids = request.data.get("equipment_ids") or []
    if not isinstance(ids, list):
        return Response({"detail": "equipment_ids must be a list.", "code": "invalid"}, status=400)
    return Response({**svc.check_acknowledgement(text, [int(i) for i in ids if str(i).isdigit()]), "stored": False})


# --------------------------------------------------------------------------- ORCID and suggestions
@impact_api(["GET", "PUT", "DELETE"])
def my_orcid(request):
    if request.method == "PUT":
        svc.set_orcid(request.user, str(request.data.get("orcid") or ""),
                      discovery_enabled=request.data.get("discovery_enabled", True) not in (False, "false", "0", 0))
    elif request.method == "DELETE":
        svc.remove_orcid(request.user)
    row = ResearcherOrcid.objects.filter(user=request.user).first()
    return Response({
        "orcid": row.orcid if row else "",
        "discovery_enabled": bool(row and row.discovery_enabled),
        "last_checked_at": row.last_checked_at.isoformat() if row and row.last_checked_at else None,
        "last_error": row.last_error if row else "",
    })


@impact_api(["GET"])
def my_suggestions(request):
    svc.sync_claimed(request.user)
    qs = PublicationSuggestion.objects.filter(user=request.user)
    status = request.query_params.get("status") or SuggestionStatus.NEW
    if status in SuggestionStatus.values:
        qs = qs.filter(status=status)
    return Response({"results": [suggestion_out(s) for s in qs[:200]]})


@impact_api(["POST"])
def my_suggestions_check(request):
    return Response(svc.discover(request.user))


@impact_api(["POST"])
def suggestion_dismiss(request, pk: int):
    s = get_object_or_404(PublicationSuggestion, pk=pk)
    return Response(suggestion_out(svc.dismiss(s, request.user)))


# --------------------------------------------------------------------------- funder report
def _report(request) -> dict:
    p = request.query_params
    ids = [int(x) for x in p.get("equipment_ids", "").split(",") if x.strip().isdigit()] or None
    return svc.funder_report(request.user, date_from=p.get("date_from"), date_to=p.get("date_to"),
                             funder=p.get("funder", ""), scheme=p.get("scheme", ""), equipment_ids=ids)


@impact_api(["GET"])
def funder_report(request):
    report = _report(request)
    fmt = request.query_params.get("export")
    if fmt not in ("csv", "xlsx"):
        return Response(report)
    header = [label for _, label in svc.EXPORT_COLUMNS]
    rows = svc.export_rows(report)
    name = f"funder-impact-{report['date_from']}-to-{report['date_to']}"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"Period {report['date_from']} to {report['date_to']}"])
        w.writerow(header)
        w.writerows(rows)
        for g in report["groups"]:
            t = g["totals"]
            w.writerow([g["funder"], g["scheme"], "", "Total", t["bookings"], t["booking_hours"], t["users_internal"],
                        t["users_external"], t["samples"], t["revenue"], t["publications_unique"], t["impact_factor_sum"],
                        t["people_authorised"], ""])
        resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        return resp
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Funder impact"
    ws.append([f"Funder impact report, {report['date_from']} to {report['date_to']}"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append(header)
    for c in ws[2]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    ws.append([])
    ws.append(["Funder", "Scheme", "Equipment", "Bookings", "Hours", "Internal users", "External users", "Samples",
                "Revenue (INR)", "Unique publications", "Impact factor (sum)", "People authorised", "Sanctioned (INR)"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for g in report["groups"]:
        t = g["totals"]
        ws.append([g["funder"], g["scheme"], t["equipment"], t["bookings"], t["booking_hours"], t["users_internal"],
                   t["users_external"], t["samples"], t["revenue"], t["publications_unique"], t["impact_factor_sum"],
                   t["people_authorised"], t["sanctioned_amount"]])
    for note in report.get("notes", []):
        ws.append([note])
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    return resp
