"""AMC / CMC / warranty / service records per equipment, renewals and expiry reminders.

The reminder sweep (:func:`send_reminders`) is idempotent: each active contract is reminded once when it enters the
department's ``amc_reminder_days`` window, and contracts past their end date are marked EXPIRED. It runs from the
``procurement_management.amc_reminders`` Celery task or the ``procurement_amc_reminders`` management command.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit, documents
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, req_reason, req_str
from .errors import ProcurementError, not_found
from .models import AMCServiceRecord, Asset, ProcurementManagementConfiguration, ProcurementRecord, Vendor
from .numbering import next_number

S = c.AMCStatus
P = c.OfficePermission


def _require(scope, dept_id):
    cfg = access.require_config(dept_id)
    if not cfg.amc_enabled:
        raise ProcurementError("AMC / service records are not enabled for this department.", code="feature_disabled")
    scope.require_perm(dept_id, P.AMC)
    return cfg


def _terms(data, dept_id, *, equipment=None) -> dict:
    start = parse_day(data.get("start_date"), "start_date", required=True)
    end = parse_day(data.get("end_date"), "end_date", required=True)
    if end < start:
        raise ProcurementError("The end date must be on or after the start date.", code="invalid_dates", field="end_date")
    value = parse_money(data.get("contract_value"), "contract_value")
    gst = parse_money(data.get("gst_amount"), "gst_amount", required=False) or Decimal("0.00")
    vendor = None
    vid = parse_int(data.get("vendor_id"), "vendor_id")
    if vid:
        vendor = Vendor.objects.filter(pk=vid, department_id=dept_id, is_archived=False).first()
        if vendor is None:
            raise not_found("Vendor not found.")
    asset = None
    aid = parse_int(data.get("asset_id"), "asset_id")
    if aid:
        asset = Asset.objects.filter(pk=aid, department_id=dept_id, is_archived=False).first()
        if asset is None:
            raise not_found("Asset not found.")
        if equipment is not None and asset.equipment_id and asset.equipment_id != equipment.pk:
            raise ProcurementError("The asset belongs to other equipment.", code="asset_mismatch", field="asset_id")
    record = None
    rid = parse_int(data.get("procurement_record_id"), "procurement_record_id")
    if rid:
        record = ProcurementRecord.objects.filter(pk=rid, department_id=dept_id, is_archived=False).first()
        if record is None:
            raise not_found("Procurement record not found.")
    return dict(
        vendor=vendor, asset=asset, procurement_record=record,
        contract_type=choice(data.get("contract_type"), c.AMCContractType.values, "contract_type", default=c.AMCContractType.AMC),
        contract_reference=req_str(data, "contract_reference", max_len=120, required=False),
        start_date=start, end_date=end, contract_value=value, gst_amount=gst, total_value=value + gst,
        coverage=req_str(data, "coverage", max_len=5000, required=False),
        status=S.ACTIVE if end >= timezone.localdate() else S.EXPIRED,
    )


@transaction.atomic
def create(scope, data: dict, files=(), *, request=None) -> AMCServiceRecord:
    from .requests_service import _lookup_equipment

    equipment = _lookup_equipment(data.get("equipment_id"))
    if equipment is None:
        raise ProcurementError("Choose the equipment this contract covers.", code="required", field="equipment_id")
    dept = access.resolve_department(scope, equipment=equipment)
    _require(scope, dept.pk)
    rec = AMCServiceRecord.objects.create(
        number=next_number(c.NumberPrefix.AMC), department=dept, equipment=equipment, created_by=scope.user,
        **_terms(data, dept.pk, equipment=equipment),
    )
    _attach(scope, rec, files, request)
    audit.record(scope.user, "amc.created", rec, new={"equipment": equipment.pk, "end_date": rec.end_date, "total": rec.total_value}, request=request)
    return rec


def _attach(scope, rec, files, request):
    for f in files or []:
        documents.create_document(
            scope, rec.department, f, doc_type=c.DocumentType.AMC_CONTRACT, links={"amc_record": rec},
            description=f"Contract {rec.number}", request=request,
        )


def _lock(rec) -> AMCServiceRecord:
    return AMCServiceRecord.objects.select_for_update(of=("self",)).select_related("department", "equipment").get(pk=rec.pk)


@transaction.atomic
def renew(scope, rec: AMCServiceRecord, data: dict, files=(), *, request=None) -> AMCServiceRecord:
    rec = _lock(rec)
    _require(scope, rec.department_id)
    if rec.status not in (S.ACTIVE, S.EXPIRED):
        raise ProcurementError("Only active or expired contracts can be renewed.", code="invalid_status")
    terms = _terms({"vendor_id": rec.vendor_id, "asset_id": rec.asset_id, "contract_type": rec.contract_type, **data}, rec.department_id, equipment=rec.equipment)
    if terms["start_date"] <= rec.start_date:
        raise ProcurementError("The renewal must start after the current contract started.", code="invalid_dates", field="start_date")
    new = AMCServiceRecord.objects.create(
        number=next_number(c.NumberPrefix.AMC), department=rec.department, laboratory=rec.laboratory, equipment=rec.equipment,
        renewed_from=rec, created_by=scope.user, **terms,
    )
    before = rec.status
    rec.status = S.RENEWED
    rec.save(update_fields=["status", "updated_at"])
    _attach(scope, new, files, request)
    audit.record(scope.user, "amc.renewed", rec, old={"status": before}, new={"status": rec.status, "renewal": new.number}, request=request)
    audit.record(scope.user, "amc.created", new, new={"renewed_from": rec.number, "end_date": new.end_date, "total": new.total_value}, request=request)
    return new


@transaction.atomic
def update(scope, rec: AMCServiceRecord, data: dict, *, request=None) -> AMCServiceRecord:
    rec = _lock(rec)
    _require(scope, rec.department_id)
    if rec.status in (S.CANCELLED, S.RENEWED):
        raise ProcurementError("This contract is closed.", code="invalid_status")
    fields = ["contract_reference", "coverage"]
    before = audit.snapshot(rec, fields)
    if "contract_reference" in data:
        rec.contract_reference = req_str(data, "contract_reference", max_len=120, required=False)
    if "coverage" in data:
        rec.coverage = req_str(data, "coverage", max_len=5000, required=False)
    rec.save(update_fields=fields + ["updated_at"])
    old, new = audit.diff(before, audit.snapshot(rec, fields))
    if new:
        audit.record(scope.user, "amc.updated", rec, old=old, new=new, request=request)
    return rec


@transaction.atomic
def cancel(scope, rec: AMCServiceRecord, data: dict, *, request=None) -> AMCServiceRecord:
    rec = _lock(rec)
    _require(scope, rec.department_id)
    if rec.status in (S.CANCELLED, S.RENEWED):
        raise ProcurementError("This contract is already closed.", code="invalid_status")
    reason = req_reason(data)
    before = rec.status
    rec.status = S.CANCELLED
    rec.save(update_fields=["status", "updated_at"])
    audit.record(scope.user, "amc.cancelled", rec, old={"status": before}, new={"status": rec.status}, reason=reason, request=request)
    return rec


def can_view(scope, rec: AMCServiceRecord) -> bool:
    if rec.department_id not in scope.department_ids():
        return False
    return scope.dept_wide(rec.department_id) or scope.is_oic_for(rec.equipment_id) or scope.is_operator_for(rec.equipment_id)


def days_left(rec, today=None) -> int:
    return (rec.end_date - (today or timezone.localdate())).days


def send_reminders(today=None) -> dict:
    """Expire lapsed contracts and remind OICs + Office (``amc``) once per contract inside the reminder window."""
    from iic_booking.communication.in_app import equipment_oic_users

    from . import notify

    today = today or timezone.localdate()
    expired = reminded = 0
    cfgs = ProcurementManagementConfiguration.objects.filter(module_enabled=True, amc_enabled=True)
    for cfg in cfgs:
        with transaction.atomic():
            lapsed = list(
                AMCServiceRecord.objects.select_for_update().filter(
                    department_id=cfg.department_id, status=S.ACTIVE, end_date__lt=today, is_archived=False
                )
            )
            for rec in lapsed:
                rec.status = S.EXPIRED
                rec.save(update_fields=["status", "updated_at"])
                audit.record(None, "amc.expired", rec, old={"status": S.ACTIVE}, new={"status": S.EXPIRED})
            expired += len(lapsed)
        horizon = today + timedelta(days=cfg.amc_reminder_days)
        due = AMCServiceRecord.objects.select_related("equipment").filter(
            department_id=cfg.department_id, status=S.ACTIVE, end_date__gte=today, end_date__lte=horizon,
            reminder_sent_at__isnull=True, is_archived=False,
        )
        for rec in due:
            with transaction.atomic():
                claimed = AMCServiceRecord.objects.filter(pk=rec.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=timezone.now())
                if not claimed:
                    continue
                users = list(equipment_oic_users(rec.equipment)) + notify.office_users(cfg.department_id, P.AMC)
                notify.notify(
                    users,
                    department_id=cfg.department_id,
                    title=f"{rec.get_contract_type_display()} expiring: {rec.equipment.name}",
                    message=f"{rec.number} ends on {rec.end_date:%d %b %Y} ({days_left(rec, today)} days left). Plan the renewal.",
                    link=f"/procurement/amc/{rec.pk}", event="amc_expiring",
                    extra={"amc_id": rec.pk, "number": rec.number, "end_date": rec.end_date.isoformat()},
                )
                audit.record(None, "amc.reminder_sent", rec, new={"end_date": rec.end_date, "recipients": len(users)})
                reminded += 1
    return {"expired": expired, "reminded": reminded}


def get_visible(scope, pk) -> AMCServiceRecord:
    rec = AMCServiceRecord.objects.select_related("department", "equipment", "vendor", "asset", "created_by").filter(pk=pk, is_archived=False).first()
    if rec is None or not can_view(scope, rec):
        raise not_found()
    return rec
