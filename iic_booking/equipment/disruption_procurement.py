"""Raise a Procurement & Assets request from a disruption (items the service person recommended).

Only for equipment whose department has the Procurement & Assets module enabled for the user (pilot rules
included) and only for users who may raise requests for that equipment. The request is created and submitted
through the module's own services, so it follows the normal approval route; its id is linked back to the event.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from decimal import InvalidOperation

from django.core.files.base import ContentFile
from django.db import transaction

logger = logging.getLogger(__name__)

CATEGORIES = (
    ("CONSUMABLE", "Consumables", "consumables_enabled"),
    ("MINOR_ASSET", "Minor assets", "minor_purchase_enabled"),
    ("MAJOR_ASSET", "Major assets", "major_purchase_enabled"),
    ("REPAIR_MAINTENANCE", "Repair / maintenance", "amc_enabled"),
    ("SERVICE", "Service / calibration", "amc_enabled"),
    ("AMC", "AMC / CMC", "amc_enabled"),
)
CATEGORY_NATURE = {"REPAIR_MAINTENANCE": "AMC_SERVICE", "SERVICE": "AMC_SERVICE", "AMC": "AMC_SERVICE"}
MAX_ITEMS = 50


class DisruptionProcurementError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def procurement_options(user, equipment) -> dict:
    """``{"available": bool, "categories": [{value, label}]}`` for raising a request on ``equipment``."""
    out = {"available": False, "categories": []}
    dept_id = getattr(equipment, "internal_department_id", None)
    if user is None or not getattr(user, "pk", None) or not dept_id:
        return out
    try:
        from iic_booking.procurement_management import access

        scope = access.scope_for(user)
        if scope.blocked or dept_id not in scope.enabled:
            return out
        if not access.can_raise_for_equipment(scope, dept_id, equipment):
            return out
        from iic_booking.procurement_management.models import RequestTypeConfig

        cfg = access.get_config(dept_id)
        active_types = set(
            RequestTypeConfig.objects.filter(department_id=dept_id, active=True).values_list("code", flat=True)
        )
        categories = [
            {"value": v, "label": label}
            for v, label, flag in CATEGORIES
            if getattr(cfg, flag, False) and (v in active_types or not active_types)
        ]
        can_record = _can_record_maintenance(scope, dept_id, equipment)
    except Exception:
        logger.exception("Could not resolve procurement options for equipment %s", getattr(equipment, "pk", None))
        return out
    return {
        "available": bool(categories),
        "categories": categories,
        "department_id": dept_id,
        "inventory_suggestions": True,
        "can_record_maintenance": can_record,
        "maintenance_kinds": _maintenance_kinds(),
    }


def _can_record_maintenance(scope, dept_id, equipment) -> bool:
    from iic_booking.procurement_management import maintenance

    return maintenance.can_record(scope, dept_id, equipment.pk)


def _maintenance_kinds() -> list[dict]:
    from iic_booking.procurement_management.constants import MaintenanceKind

    return [{"value": k.value, "label": str(k.label)} for k in MaintenanceKind]


def _decimal(raw, *, field: str, allow_zero: bool) -> Decimal:
    try:
        value = Decimal(str(raw if raw not in (None, "") else "0").strip())
    except (InvalidOperation, ValueError):
        raise DisruptionProcurementError(f"Enter a number for {field}.")
    if value < 0 or (not allow_zero and value <= 0) or not value.is_finite():
        raise DisruptionProcurementError(f"Enter a valid {field}.")
    return value


def _clean_items(raw_items) -> list[dict]:
    if not isinstance(raw_items, list) or not raw_items:
        raise DisruptionProcurementError("Add at least one item.")
    if len(raw_items) > MAX_ITEMS:
        raise DisruptionProcurementError(f"Add at most {MAX_ITEMS} items.")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise DisruptionProcurementError("Each item needs a name and quantity.")
        name = str(raw.get("name") or "").replace("\x00", "").strip()[:255]
        item_id = raw.get("item_id")
        try:
            item_id = int(item_id) if item_id not in (None, "") else None
        except (TypeError, ValueError):
            raise DisruptionProcurementError("Unknown inventory item.")
        if not name and item_id is None:
            raise DisruptionProcurementError("Each item needs a name.")
        items.append(
            {
                "item_id": item_id,
                "name": name,
                "quantity": _decimal(raw.get("quantity"), field="quantity", allow_zero=False),
                "estimated_cost": _decimal(raw.get("estimated_cost"), field="estimated cost", allow_zero=True),
                "recommended": bool(raw.get("recommended_by_service_person", True)),
                "notes": str(raw.get("notes") or "").replace("\x00", "").strip()[:2000],
            }
        )
    return items


def _event_summary(event) -> str:
    from django.utils import timezone

    from .disruption_service import DISRUPTION_TYPE_LABELS

    eq = event.equipment
    name = f"{eq.name} ({eq.code})" if getattr(eq, "code", "") else eq.name
    start = timezone.localtime(event.start_at).strftime("%d %b %Y") if event.start_at else ""
    label = DISRUPTION_TYPE_LABELS.get(event.disruption_type, event.disruption_type)
    return f"disruption #{event.pk} ({label}, from {start}) on {name}"


def _attach_reports(scope, purchase_request, event, request) -> tuple[int, int]:
    from iic_booking.procurement_management import constants as pc
    from iic_booking.procurement_management import documents

    attached = skipped = 0
    for report in event.service_reports.all():
        ext = os.path.splitext(report.original_name or "")[1].lower()
        if ext not in pc.ALLOWED_DOCUMENT_EXTENSIONS or (report.size_bytes or 0) > documents.max_bytes():
            skipped += 1
            continue
        try:
            with report.file.open("rb") as fh:
                content = ContentFile(fh.read(), name=report.original_name)
            documents.create_document(
                scope,
                purchase_request.department,
                content,
                doc_type=pc.DocumentType.OTHER,
                links={"purchase_request": purchase_request},
                description=f"Service report (disruption #{event.pk})",
                request=request,
            )
            attached += 1
        except Exception:
            logger.exception("Could not copy service report %s to procurement request", report.pk)
            skipped += 1
    return attached, skipped


def raise_procurement_request(event, user, data, *, request=None) -> dict:
    from iic_booking.procurement_management import access
    from iic_booking.procurement_management import requests_service
    from iic_booking.procurement_management import workflow
    from iic_booking.procurement_management.errors import ProcurementError
    from iic_booking.procurement_management.models import ItemCategory

    from .disruption_service import _log_edit

    data = data or {}
    equipment = event.equipment
    options = procurement_options(user, equipment)
    if not options["available"]:
        raise DisruptionProcurementError(
            "Procurement & Assets is not available for this equipment's department.", status=403
        )
    category = str(data.get("category") or "").strip().upper()
    labels = {c["value"]: c["label"] for c in options["categories"]}
    if category not in labels:
        raise DisruptionProcurementError("Choose what the request is for (consumables, assets, repair, service or AMC).")
    items = _clean_items(data.get("items"))
    notes = str(data.get("notes") or "").replace("\x00", "").strip()[:4000]
    summary = _event_summary(event)
    recommended = any(i["recommended"] for i in items)
    justification = "\n\n".join(
        p
        for p in (
            f"Raised after resuming {summary}."
            + (" The items were recommended by the service person." if recommended else ""),
            notes,
        )
        if p
    )
    lines = []
    for i in items:
        spec = "\n".join(
            p for p in (i["notes"], "Recommended by the service person." if i["recommended"] else "") if p
        )
        line = {
            "description": i["name"],
            "quantity": str(i["quantity"]),
            "estimated_unit_price": str(i["estimated_cost"]),
            "specification": spec,
        }
        if i["item_id"]:
            line["item_id"] = i["item_id"]
        lines.append(line)
    payload = {
        "equipment_id": equipment.pk,
        "request_type": category,
        "title": f"{labels[category]} after service — {equipment.name}"[:255],
        "justification": justification,
        "specification": f"Items needed after the service of {summary}.",
        "lines": lines,
    }
    cat = (
        ItemCategory.objects.filter(
            department_id=equipment.internal_department_id, nature=CATEGORY_NATURE.get(category, category), active=True
        )
        .order_by("id")
        .first()
    )
    if cat is not None:
        payload["category_id"] = cat.pk
    scope = access.scope_for(user)
    submitted, submit_error = False, ""
    maintenance_number = ""
    try:
        with transaction.atomic():
            pr = requests_service.create_request(scope, payload, request=request)
            pr.disruption_event = event
            maint = _maintenance_for(scope, event, data.get("maintenance"), request)
            if maint is not None:
                pr.maintenance_record = maint
                maintenance_number = maint.number
            pr.save(update_fields=["disruption_event", "maintenance_record", "updated_at"])
            attached, skipped = (0, 0)
            if data.get("attach_service_reports", True):
                attached, skipped = _attach_reports(scope, pr, event, request)
            if data.get("submit", True):
                try:
                    with transaction.atomic():
                        pr = workflow.submit(scope, pr, request=request)
                    submitted = True
                except ProcurementError as exc:
                    submit_error = exc.message
            event.procurement_request_ids = [*(event.procurement_request_ids or []), pr.pk]
            event.save(update_fields=["procurement_request_ids", "updated_at"])
            _log_edit(
                event,
                "procurement",
                user,
                note=f"Procurement request {pr.number} raised ({labels[category].lower()})"[:255],
            )
    except ProcurementError as exc:
        raise DisruptionProcurementError(exc.message, status=exc.status)
    return {
        "id": pr.pk,
        "number": pr.number,
        "status": pr.status,
        "status_display": pr.get_status_display(),
        "submitted": submitted,
        "submit_error": submit_error,
        "reports_attached": attached,
        "reports_skipped": skipped,
        "maintenance_record": maintenance_number,
    }


def _maintenance_for(scope, event, raw, request):
    """Reuse the event's maintenance record or create one when the prompt asked to record maintenance."""
    from iic_booking.procurement_management import maintenance
    from iic_booking.procurement_management.models import MaintenanceRecord

    existing = MaintenanceRecord.objects.filter(disruption_event=event, is_archived=False).order_by("-id").first()
    if not isinstance(raw, dict) or not raw:
        return existing
    if existing is not None:
        return maintenance.update(scope, existing, raw, request=request)
    return maintenance.create(scope, raw, request=request, disruption_event=event)

