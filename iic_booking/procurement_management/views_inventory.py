"""Asset registers, bulk import, QR labels, physical verification, disposal, equipment ↔ item links, maintenance,
OC Stores line edits, purchase mode and the Accounts bill queue."""

from __future__ import annotations

from django.db.models import Q
from rest_framework.response import Response

from . import (
    access,
    asset_import,
    item_links,
    maintenance,
    printables,
    purchase_mode,
    registers,
    store_lines,
    verification,
)
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_bool, parse_int, pm_api
from .errors import ProcurementError, not_found
from .exports import table_response
from .models import Asset, AssetRegister, Invoice, VerificationCampaign
from .views_assets import _assets_qs, _get_asset, assets_q

P = c.OfficePermission

ENTRY_HEADERS = [
    "Register", "Page", "Serial", "Entry date", "Asset tag", "Asset no.", "Description", "Make / model", "Mfr. serial",
    "Qty", "Equipment", "Location", "Supplier", "PO no.", "PO date", "Invoice no.", "Invoice date", "Cost (₹)",
    "Funding", "Condition", "Status", "Last verified", "Verification",
]


def entry_row(a) -> list:
    return [
        a.register.code if a.register_id else "", a.register_page or "", a.register_serial, s.iso(a.register_entry_date) or "",
        a.asset_tag, a.number, a.description, " ".join(x for x in (a.make, a.model_number) if x), a.serial_number,
        a.quantity, a.equipment.name if a.equipment_id else "", a.location, a.supplier_name, a.po_number,
        s.iso(a.po_date) or "", a.invoice_number, s.iso(a.invoice_date) or "", s.m(a.cost),
        a.funding_source or a.get_funding_type_display(), a.get_condition_display() if a.condition else "",
        a.get_status_display(), s.iso(a.last_verified_on) or "",
        a.get_last_verification_result_display() if a.last_verification_result else "",
    ]


def _entries_qs():
    return _assets_qs().select_related("register", "parent")


def _can_export(scope) -> bool:
    return any(scope.has_perm(d, P.REPORTS) or scope.has_perm(d, P.ASSETS) for d in scope.department_ids())


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------
@pm_api(["GET", "POST"])
def registers_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        reg = registers.save_register(scope, data_of(request), request=request)
        return Response(s.asset_register(registers.registers_qs().get(pk=reg.pk), entries=0), status=201)
    p = request.query_params
    qs = registers.with_entry_counts(registers.registers_qs().filter(department_id__in=scope.department_ids()))
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    if p.get("register_type"):
        qs = qs.filter(register_type__in=p["register_type"].split(","))
    if p.get("active") not in (None, ""):
        qs = qs.filter(active=parse_bool(p["active"]))
    rows = [s.asset_register(r, entries=r.entry_total) for r in qs.order_by("register_type", "code")]
    return Response({"results": rows, "count": len(rows)})


@pm_api(["GET", "PATCH"])
def register_detail(request, pk: int):
    scope = request.pm_scope
    reg = registers.get_visible_register(scope, pk)
    if request.method == "PATCH":
        reg = registers.save_register(scope, data_of(request), reg, request=request)
    reg = registers.with_entry_counts(registers.registers_qs().filter(pk=reg.pk)).get()
    pages = (
        Asset.objects.filter(register=reg, is_archived=False, register_page__isnull=False)
        .order_by("register_page").values_list("register_page", flat=True).distinct()
    )
    out = s.asset_register(reg, entries=reg.entry_total)
    out["pages_used"] = list(pages)
    out["next_free"] = dict(zip(("page", "serial"), registers.next_free_entry(reg)))
    return Response(out)


@pm_api(["GET"])
def register_entries(request, pk: int):
    scope = request.pm_scope
    reg = registers.get_visible_register(scope, pk)
    p = request.query_params
    qs = _entries_qs().filter(assets_q(scope), register=reg)
    page = parse_int(p.get("page_no"), "page_no")
    if page:
        qs = qs.filter(register_page=page)
    if p.get("serial"):
        qs = qs.filter(register_serial__iexact=p["serial"].strip())
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(registers.search_q(term))
    qs = qs.order_by("register_page", "register_serial", "id")
    fmt = p.get("export")
    if fmt:
        if not _can_export(scope):
            raise not_found()
        if fmt == "pdf":
            return printables.register_pdf(reg, list(qs.select_related("custodian", "laboratory", "vendor")[:5000]))
        return table_response(fmt, f"Register {reg.code} — {reg.name}", ENTRY_HEADERS, [entry_row(a) for a in qs[:5000]],
                              subtitle=f"{reg.department.name} — {reg.get_register_type_display()}")
    return paginate(request, qs, s.asset)


# ---------------------------------------------------------------------------
# Asset lookup, labels, verification, disposal
# ---------------------------------------------------------------------------
@pm_api(["GET"])
def asset_lookup(request):
    """Find one asset by tag / asset number (QR scan) or by register, page and serial."""
    scope = request.pm_scope
    p = request.query_params
    asset = registers.lookup(
        scope,
        tag=(p.get("tag") or "").strip(),
        register_id=parse_int(p.get("register_id"), "register_id"),
        page=parse_int(p.get("page_no"), "page_no"),
        serial=(p.get("serial") or "").strip(),
    )
    if asset is None:
        raise not_found("No asset matches.")
    out = s.asset(_entries_qs().get(pk=asset.pk), detail=True)
    out["can_verify"] = verification.can_verify(scope, asset)
    out["open_campaigns"] = [
        {"id": x.pk, "number": x.number, "title": x.title}
        for x in VerificationCampaign.objects.filter(department_id=asset.department_id, status=c.CampaignStatus.OPEN)
        .filter(Q(register__isnull=True) | Q(register_id=asset.register_id))
        .filter(Q(laboratory__isnull=True) | Q(laboratory_id=asset.laboratory_id))
    ]
    return Response(out)


@pm_api(["GET", "POST"])
def asset_labels(request):
    scope = request.pm_scope
    src = data_of(request) if request.method == "POST" else request.query_params
    qs = _entries_qs().filter(assets_q(scope)).select_related("department")
    raw_ids = src.get("asset_ids") if request.method == "POST" else (src.get("ids") or "").split(",")
    ids = [int(x) for x in (raw_ids or []) if str(x).strip().isdigit()]
    reg_id = parse_int(src.get("register_id"), "register_id")
    if ids:
        qs = qs.filter(pk__in=ids)
    elif reg_id:
        qs = qs.filter(register_id=reg_id)
        page = parse_int(src.get("page_no"), "page_no")
        if page:
            qs = qs.filter(register_page=page)
    else:
        raise ProcurementError("Choose the assets or a register to print labels for.", code="required", field="asset_ids")
    assets = list(qs.order_by("register_id", "register_page", "register_serial", "id")[:1000])
    if not assets:
        raise not_found("No assets to print.")
    for a in assets:
        registers.ensure_tag(a)
    return printables.labels_pdf(assets)


@pm_api(["GET", "POST"])
def asset_verifications(request, pk: int):
    scope = request.pm_scope
    a = _get_asset(request, pk)
    if request.method == "POST":
        row = verification.verify(scope, a, data_of(request), request=request)
        return Response(s.asset_verification(row), status=201)
    return Response({"results": [s.asset_verification(v) for v in a.verifications.select_related("verified_by", "campaign")]})


@pm_api(["POST"])
def asset_dispose(request, pk: int):
    row = registers.dispose(request.pm_scope, _get_asset(request, pk), data_of(request), request=request)
    out = s.asset(_entries_qs().get(pk=row.asset_id), detail=True)
    return Response({"disposal": s.asset_disposal(row), "asset": out}, status=201)


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------
@pm_api(["GET"])
def import_template(request):
    return asset_import.template_response(request.query_params.get("type") or "xlsx")


@pm_api(["POST"])
def import_preview(request):
    return Response(asset_import.preview(request.pm_scope, data_of(request), request.FILES.get("file")))


@pm_api(["POST"])
def import_commit(request):
    return Response(asset_import.commit(request.pm_scope, data_of(request), request.FILES.get("file"), request=request), status=201)


# ---------------------------------------------------------------------------
# Physical verification campaigns
# ---------------------------------------------------------------------------
def _get_campaign(scope, pk) -> VerificationCampaign:
    cmp = verification.campaigns_qs().filter(pk=pk, department_id__in=scope.department_ids()).first()
    if cmp is None:
        raise not_found("Campaign not found.")
    return cmp


@pm_api(["GET", "POST"])
def campaigns(request):
    scope = request.pm_scope
    if request.method == "POST":
        cmp = verification.create_campaign(scope, data_of(request), request=request)
        return Response(s.verification_campaign(cmp, stats=verification.campaign_stats(cmp)), status=201)
    qs = verification.campaigns_qs().filter(department_id__in=scope.department_ids())
    if request.query_params.get("status"):
        qs = qs.filter(status=request.query_params["status"])
    return paginate(request, qs, lambda x: s.verification_campaign(x, stats=verification.campaign_stats(x)))


@pm_api(["GET"])
def campaign_detail(request, pk: int):
    cmp = _get_campaign(request.pm_scope, pk)
    return Response(s.verification_campaign(cmp, stats=verification.campaign_stats(cmp)))


@pm_api(["POST"])
def campaign_close(request, pk: int):
    cmp = verification.close_campaign(request.pm_scope, _get_campaign(request.pm_scope, pk), data_of(request), request=request)
    return Response(s.verification_campaign(cmp, stats=verification.campaign_stats(cmp)))


@pm_api(["GET"])
def campaign_assets(request, pk: int):
    """``?state=pending`` (not yet sighted) or ``?state=done`` (with the latest result) for the campaign."""
    scope = request.pm_scope
    cmp = _get_campaign(scope, pk)
    state = request.query_params.get("state") or "pending"
    if state == "done":
        from .models import AssetVerification

        qs = AssetVerification.objects.filter(campaign=cmp).select_related("asset", "asset__register", "verified_by", "campaign")
        result = request.query_params.get("result")
        if result:
            qs = qs.filter(result=result)
        fmt = request.query_params.get("export")
        if fmt:
            if not _can_export(scope):
                raise not_found()
            headers = ["Register ref", "Asset tag", "Asset no.", "Description", "Result", "Condition", "Qty found",
                       "Location seen", "Verified on", "Verified by", "Remarks"]
            rows = [[v.asset.register_ref, v.asset.asset_tag, v.asset.number, v.asset.description, v.get_result_display(),
                     v.get_condition_display() if v.condition else "", v.quantity_found or "", v.location_seen,
                     s.iso(v.verified_on), (s.user_brief(v.verified_by) or {}).get("name", ""), v.remarks]
                    for v in qs.order_by("asset__register__code", "asset__register_page", "asset__register_serial")[:5000]]
            return table_response(fmt, f"Physical verification {cmp.number}", headers, rows, subtitle=cmp.title)

        def ser(v):
            return {**s.asset_verification(v), "asset": {"id": v.asset_id, "number": v.asset.number,
                    "description": v.asset.description, "asset_tag": v.asset.asset_tag, "register_ref": v.asset.register_ref}}
        return paginate(request, qs.order_by("-verified_on", "-id"), ser)
    qs = verification.pending_assets(cmp).select_related(
        "register", "department", "laboratory", "equipment", "category", "procurement_record", "vendor", "custodian",
        "created_by", "parent",
    ).order_by("register__code", "register_page", "register_serial", "id")
    fmt = request.query_params.get("export")
    if fmt:
        if not _can_export(scope):
            raise not_found()
        return table_response(fmt, f"Not yet verified — {cmp.number}", ENTRY_HEADERS, [entry_row(a) for a in qs[:5000]], subtitle=cmp.title)
    return paginate(request, qs, s.asset)


# ---------------------------------------------------------------------------
# Equipment ↔ item links and equipment views
# ---------------------------------------------------------------------------
@pm_api(["GET", "POST"])
def item_links_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        link = item_links.save_link(scope, data_of(request), request=request)
        return Response(s.item_link(item_links.links_qs().get(pk=link.pk)), status=201)
    p = request.query_params
    qs = item_links.links_qs().filter(department_id__in=scope.department_ids())
    for key in ("equipment_id", "item_id", "department_id"):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{key: val})
    if p.get("usage"):
        qs = qs.filter(usage=p["usage"])
    return paginate(request, qs.order_by("equipment__name", "usage", "item__name"), s.item_link)


@pm_api(["PATCH", "DELETE"])
def item_link_detail(request, pk: int):
    scope = request.pm_scope
    link = item_links.get_link(scope, pk)
    if request.method == "DELETE":
        item_links.delete_link(scope, link, request=request)
        return Response(status=204)
    link = item_links.save_link(scope, data_of(request), link, request=request)
    return Response(s.item_link(item_links.links_qs().get(pk=link.pk)))


def _equipment(pk):
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.filter(pk=pk).first()
    if eq is None:
        raise not_found("Equipment not found.")
    return eq


@pm_api(["GET"])
def equipment_suggested_lines(request, pk: int):
    return Response(item_links.suggested_lines(request.pm_scope, _equipment(pk)))


@pm_api(["GET"])
def equipment_overview(request, pk: int):
    return Response(maintenance.equipment_overview(request.pm_scope, _equipment(pk)))


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
@pm_api(["GET", "POST"])
def maintenance_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        rec = maintenance.create(scope, data_of(request), request=request)
        return Response(s.maintenance_record(maintenance.records_qs().get(pk=rec.pk), detail=True), status=201)
    p = request.query_params
    qs = maintenance.records_qs().filter(maintenance.visible_q(scope))
    for key in ("equipment_id", "department_id", "asset_id", "disruption_event_id"):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{key: val})
    if p.get("kind"):
        qs = qs.filter(kind__in=p["kind"].split(","))
    if p.get("date_from"):
        qs = qs.filter(downtime_start__date__gte=p["date_from"])
    if p.get("date_to"):
        qs = qs.filter(downtime_start__date__lte=p["date_to"])
    qs = qs.order_by("-downtime_start", "-created_at")
    fmt = p.get("export")
    if fmt:
        headers = ["Record", "Equipment", "Kind", "Downtime from", "Downtime to", "Hours", "Cause", "Action taken",
                   "Service provider", "Service cost (₹)", "Parts cost (₹)", "Other cost (₹)", "Total (₹)", "Recorded by"]
        rows = [[r.number, r.equipment.name, r.get_kind_display(), s.iso(r.downtime_start) or "", s.iso(r.downtime_end) or "",
                 r.downtime_hours or "", r.cause, r.action_taken, r.service_provider or (r.vendor.name if r.vendor_id else ""),
                 s.m(r.service_cost), s.m(r.parts_cost), s.m(r.other_cost), s.m(r.total_cost),
                 (s.user_brief(r.recorded_by) or {}).get("name", "")] for r in qs[:5000]]
        return table_response(fmt, "Maintenance history", headers, rows)
    return paginate(request, qs, s.maintenance_record)


@pm_api(["GET", "PATCH"])
def maintenance_detail(request, pk: int):
    scope = request.pm_scope
    rec = maintenance.get_visible(scope, pk)
    if request.method == "PATCH":
        rec = maintenance.update(scope, rec, data_of(request), request=request)
    out = s.maintenance_record(maintenance.records_qs().get(pk=rec.pk), detail=True)
    out["can_edit"] = maintenance.can_record(scope, rec.department_id, rec.equipment_id)
    return Response(out)


@pm_api(["POST"])
def maintenance_raise_request(request, pk: int):
    from .views_requests import _base_qs

    scope = request.pm_scope
    rec = maintenance.get_visible(scope, pk)
    pr, submitted, error = maintenance.raise_request(scope, rec, data_of(request), request=request)
    return Response(
        {"request": s.purchase_request(_base_qs().get(pk=pr.pk), detail=True, scope=scope), "submitted": submitted,
         "submit_error": error},
        status=201,
    )


# ---------------------------------------------------------------------------
# OC Stores line edits and stock check on a request
# ---------------------------------------------------------------------------
@pm_api(["POST"])
def request_stores_edit(request, pk: int):
    from .views_requests import _detail, _get_request

    data = data_of(request)
    r = store_lines.edit_lines(
        request.pm_scope, _get_request(request, pk), lines=data.get("lines"), comments=str(data.get("comments") or "")[:2000],
        request=request,
    )
    return _detail(request, r)


@pm_api(["GET"])
def request_stock_check(request, pk: int):
    from .views_requests import _get_request

    r = _get_request(request, pk)
    return Response({"results": store_lines.suggest_fulfilment(r)})


# ---------------------------------------------------------------------------
# Purchase mode and bills for Accounts
# ---------------------------------------------------------------------------
@pm_api(["GET", "POST"])
def record_purchase_mode(request, pk: int):
    from .views_procurement import get_record, record_response

    rec = get_record(request, pk)
    if request.method == "POST":
        rec = purchase_mode.set_mode(request.pm_scope, rec, data_of(request), request=request)
        return record_response(request, rec)
    return Response(purchase_mode.for_record(rec))


@pm_api(["POST"])
def invoice_forward(request, pk: int):
    from .views_procurement import _get_invoice

    inv = purchase_mode.forward_invoice(request.pm_scope, _get_invoice(request, pk), data_of(request), request=request)
    return Response(s.invoice(Invoice.objects.select_related("vendor", "recorded_by", "forwarded_by").get(pk=inv.pk), detail=True))


@pm_api(["GET"])
def accounts_bills(request):
    """Bills forwarded to Accounts: ``?state=pending`` (unpaid / part-paid, default), ``paid`` or ``all``."""
    scope = request.pm_scope
    depts = [d for d in scope.department_ids() if scope.has_perm(d, P.PAYMENTS) or scope.has_perm(d, P.INVOICES) or scope.dept_wide(d)]
    qs = Invoice.objects.select_related("procurement_record", "vendor", "recorded_by", "forwarded_by").filter(
        department_id__in=depts, is_archived=False, forwarded_to_accounts_at__isnull=False
    )
    state = request.query_params.get("state") or "pending"
    if state == "pending":
        qs = qs.exclude(payment_status=c.PaymentStatus.PAID)
    elif state == "paid":
        qs = qs.filter(payment_status=c.PaymentStatus.PAID)

    def ser(inv):
        out = s.invoice(inv)
        rec = inv.procurement_record
        out["procurement_record"] = {"id": rec.pk, "number": rec.number, "title": rec.title}
        return out

    qs = qs.order_by("forwarded_to_accounts_at", "id")
    fmt = request.query_params.get("export")
    if fmt:
        headers = ["Record", "Title", "Vendor", "Invoice no.", "Invoice date", "Amount (₹)", "Paid (₹)", "Payment status",
                   "Forwarded on", "Forwarded by"]
        rows = [[i.procurement_record.number, i.procurement_record.title, i.vendor.name if i.vendor_id else i.vendor_name_text,
                 i.invoice_number, s.iso(i.invoice_date), s.m(i.total_amount), s.m(i.paid_amount), i.get_payment_status_display(),
                 s.iso(i.forwarded_to_accounts_at), (s.user_brief(i.forwarded_by) or {}).get("name", "")] for i in qs[:5000]]
        return table_response(fmt, "Bills with Accounts", headers, rows)
    return paginate(request, qs, ser)
