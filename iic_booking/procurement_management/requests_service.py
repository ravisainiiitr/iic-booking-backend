"""Purchase request drafting: department derivation, feature checks, line maths and edits.

Submission and every later transition live in ``workflow``.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, parse_qty, parse_rate, req_str
from .errors import ProcurementError, forbidden
from .fy import fy_label
from .models import Item, ItemCategory, PurchaseRequest, PurchaseRequestLine, RequestTypeConfig
from .numbering import next_number

R = c.ModuleRole
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

NATURE_FLAG = {
    c.ItemNature.CONSUMABLE: "consumables_enabled",
    c.ItemNature.NON_CONSUMABLE: "non_consumables_enabled",
    c.ItemNature.LIMITED_LIFE_ASSET: "limited_life_enabled",
    c.ItemNature.MINOR_ASSET: "minor_purchase_enabled",
    c.ItemNature.MAJOR_ASSET: "major_purchase_enabled",
    c.ItemNature.AMC_SERVICE: "amc_enabled",
    c.ItemNature.GENERAL_OFFICE: "general_purchase_enabled",
}
REQUEST_TYPE_FLAG = {c.RequestTypeCode.PLAN_GRANT: "plan_enabled", c.RequestTypeCode.NON_PLAN: "non_plan_enabled"}
FUNDING_FLAG = {c.FundingType.PLAN: "plan_enabled", c.FundingType.NON_PLAN: "non_plan_enabled"}
SPEC_NATURES = frozenset({c.ItemNature.NON_CONSUMABLE}) | c.ASSET_NATURES
EDITABLE_STATUSES = frozenset({c.RequestStatus.DRAFT, c.RequestStatus.REJECTED})
HEADER_FIELDS = (
    "title",
    "justification",
    "specification",
    "required_by",
    "priority",
    "funding_type",
    "request_type",
    "category",
    "nature",
    "equipment",
    "laboratory",
    "estimated_total",
)


def line_total(quantity: Decimal, unit_price: Decimal, gst_rate: Decimal) -> Decimal:
    base = quantity * unit_price
    return (base + base * gst_rate / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def check_features(cfg, *, request_type=None, nature: str = "", funding_type: str = "") -> None:
    flags = []
    if nature in NATURE_FLAG:
        flags.append(NATURE_FLAG[nature])
    if request_type is not None and request_type.code in REQUEST_TYPE_FLAG:
        flags.append(REQUEST_TYPE_FLAG[request_type.code])
    if funding_type in FUNDING_FLAG:
        flags.append(FUNDING_FLAG[funding_type])
    for flag in flags:
        if not getattr(cfg, flag):
            raise ProcurementError(
                "This kind of request is switched off for the department.", code="feature_disabled", feature=flag
            )


def _lookup_equipment(raw):
    eid = parse_int(raw, "equipment_id")
    if not eid:
        return None
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.filter(pk=eid).first()
    if eq is None:
        raise ProcurementError("Unknown equipment.", code="invalid_equipment", field="equipment_id")
    return eq


def _lookup_laboratory(raw):
    if raw in (None, ""):
        return None
    from iic_booking.sync.models import Laboratory

    try:
        lab_id = uuid.UUID(str(raw))
    except ValueError:
        raise ProcurementError("Unknown laboratory.", code="invalid_laboratory", field="laboratory_id")
    lab = Laboratory.objects.filter(pk=lab_id, is_active=True).first()
    if lab is None:
        raise ProcurementError("Unknown laboratory.", code="invalid_laboratory", field="laboratory_id")
    return lab


def _lookup_request_type(dept, data) -> RequestTypeConfig:
    qs = RequestTypeConfig.objects.filter(department=dept, active=True)
    if data.get("request_type_id") not in (None, ""):
        rt = qs.filter(pk=parse_int(data.get("request_type_id"), "request_type_id")).first()
    else:
        rt = qs.filter(code=str(data.get("request_type") or "")).first()
    if rt is None:
        raise ProcurementError("Choose an active request type.", code="invalid_request_type", field="request_type_id")
    return rt


def _lookup_category(dept, raw) -> ItemCategory | None:
    if raw in (None, ""):
        return None
    cat = ItemCategory.objects.filter(department=dept, pk=parse_int(raw, "category_id"), active=True).first()
    if cat is None:
        raise ProcurementError("Choose an active category.", code="invalid_category", field="category_id")
    return cat


def build_lines(dept, raw_lines) -> list[PurchaseRequestLine]:
    if raw_lines in (None, ""):
        return []
    if not isinstance(raw_lines, list):
        raise ProcurementError("lines must be a list.", code="invalid", field="lines")
    if len(raw_lines) > 200:
        raise ProcurementError("Too many lines (max 200).", code="invalid", field="lines")
    out = []
    for idx, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise ProcurementError("Each line must be an object.", code="invalid", field=f"lines[{idx}]")
        item = None
        if raw.get("item_id") not in (None, ""):
            item = Item.objects.filter(
                pk=parse_int(raw.get("item_id"), "item_id"), department=dept, is_archived=False, active=True
            ).select_related("default_gst_rate").first()
            if item is None:
                raise ProcurementError("Unknown item.", code="invalid_item", field=f"lines[{idx}].item_id")
        description = str(raw.get("description") or (item.name if item else "")).strip()
        if not description:
            raise ProcurementError("Each line needs a description or item.", code="required", field=f"lines[{idx}].description")
        quantity = parse_qty(raw.get("quantity"), f"lines[{idx}].quantity")
        price = parse_money(raw.get("estimated_unit_price"), f"lines[{idx}].estimated_unit_price")
        if raw.get("gst_rate") in (None, "") and item and item.default_gst_rate_id:
            gst = item.default_gst_rate.rate
        else:
            gst = parse_rate(raw.get("gst_rate"), f"lines[{idx}].gst_rate")
        out.append(
            PurchaseRequestLine(
                item=item,
                description=description[:255],
                specification=str(raw.get("specification") or (item.specification if item else ""))[:10000],
                quantity=quantity,
                uom=str(raw.get("uom") or (item.uom if item else "Nos"))[:30],
                estimated_unit_price=price,
                gst_rate=gst,
                line_total=line_total(quantity, price, gst),
            )
        )
    return out


def _replace_lines(r: PurchaseRequest, lines: list[PurchaseRequestLine]) -> None:
    r.lines.all().delete()
    for line in lines:
        line.request = r
    PurchaseRequestLine.objects.bulk_create(lines)
    r.estimated_total = sum((line.line_total for line in lines), Decimal("0.00"))


def _apply_header(r: PurchaseRequest, data: dict, *, creating: bool) -> None:
    if creating or "title" in data:
        r.title = req_str(data, "title")
    for f, max_len in (("justification", 10000), ("specification", 20000)):
        if f in data:
            setattr(r, f, req_str(data, f, max_len=max_len, required=False))
    if "required_by" in data:
        r.required_by = parse_day(data.get("required_by"), "required_by")
    if creating or "priority" in data:
        r.priority = choice(data.get("priority"), c.Priority.values, "priority", default=c.Priority.NORMAL)
    if creating or "funding_type" in data:
        default = c.FundingType.OTHER
        if r.request_type.code == c.RequestTypeCode.PLAN_GRANT:
            default = c.FundingType.PLAN
        elif r.request_type.code == c.RequestTypeCode.NON_PLAN:
            default = c.FundingType.NON_PLAN
        r.funding_type = choice(data.get("funding_type"), c.FundingType.values, "funding_type", default=default)


@transaction.atomic
def create_request(scope, data: dict, *, request=None) -> PurchaseRequest:
    equipment = _lookup_equipment(data.get("equipment_id"))
    laboratory = _lookup_laboratory(data.get("laboratory_id"))
    dept = access.resolve_department(scope, equipment=equipment, laboratory=laboratory, department_id=data.get("department_id"))
    cfg = access.require_config(dept)
    if not access.can_raise_for_equipment(scope, dept.pk, equipment):
        raise forbidden("You can only raise requests for equipment you operate or manage.")
    role = access.raising_role(scope, dept.pk, equipment)
    if role in (R.OIC, R.LAB_OPERATOR) and equipment is None:
        raise ProcurementError(
            "Choose the equipment this request is for.", code="equipment_required", field="equipment_id"
        )
    from .defaults import ensure_department_defaults

    ensure_department_defaults(dept)
    rt = _lookup_request_type(dept, data)
    category = _lookup_category(dept, data.get("category_id"))
    nature = category.nature if category else rt.default_nature
    r = PurchaseRequest(
        department=dept,
        laboratory=laboratory,
        equipment=equipment,
        request_type=rt,
        category=category,
        nature=nature,
        financial_year=fy_label(timezone.localdate()),
        requested_by=scope.user,
        raised_as_role=role,
    )
    _apply_header(r, data, creating=True)
    check_features(cfg, request_type=rt, nature=nature, funding_type=r.funding_type)
    lines = build_lines(dept, data.get("lines"))
    r.number = next_number(c.NumberPrefix.REQUEST)
    r.save()
    _replace_lines(r, lines)
    r.save(update_fields=["estimated_total", "updated_at"])
    audit.record(scope.user, "request.created", r, new=audit.snapshot(r, HEADER_FIELDS), request=request)
    return r


@transaction.atomic
def update_request(scope, r: PurchaseRequest, data: dict, *, request=None) -> PurchaseRequest:
    r = PurchaseRequest.objects.select_for_update().get(pk=r.pk)
    if r.requested_by_id != scope.user.pk:
        raise forbidden("Only the requester can edit this request.")
    cfg = access.require_config(r.department)
    if r.status not in EDITABLE_STATUSES or (r.status == c.RequestStatus.REJECTED and not cfg.allow_resubmission):
        raise ProcurementError("This request can no longer be edited.", code="not_editable")
    before = audit.snapshot(r, HEADER_FIELDS)
    if "request_type_id" in data or "request_type" in data:
        r.request_type = _lookup_request_type(r.department, data)
    if "category_id" in data:
        r.category = _lookup_category(r.department, data.get("category_id"))
    r.nature = r.category.nature if r.category_id else r.request_type.default_nature
    if "laboratory_id" in data:
        lab = _lookup_laboratory(data.get("laboratory_id"))
        if lab is not None and lab.department_id != r.department_id:
            raise ProcurementError("The laboratory belongs to a different department.", code="lab_mismatch")
        r.laboratory = lab
    _apply_header(r, data, creating=False)
    check_features(cfg, request_type=r.request_type, nature=r.nature, funding_type=r.funding_type)
    if "lines" in data:
        _replace_lines(r, build_lines(r.department, data.get("lines")))
    r.save()
    old, new = audit.diff(before, audit.snapshot(r, HEADER_FIELDS))
    if "lines" in data:
        new["lines_replaced"] = r.lines.count()
    audit.record(scope.user, "request.updated", r, old=old, new=new, request=request)
    return r


def validate_for_submit(r: PurchaseRequest, cfg) -> None:
    """Completeness checks applied when a draft (or rejected request) is submitted."""
    check_features(cfg, request_type=r.request_type, nature=r.nature, funding_type=r.funding_type)
    if not r.request_type.active:
        raise ProcurementError("This request type is no longer active.", code="invalid_request_type")
    lines = list(r.lines.all())
    if not lines:
        raise ProcurementError("Add at least one line item.", code="lines_required", field="lines")
    if r.estimated_total <= 0:
        raise ProcurementError("The estimated total must be more than zero.", code="invalid_total")
    if not r.justification.strip():
        raise ProcurementError("A justification is required.", code="required", field="justification")
    needs_spec = r.request_type.requires_specification or r.nature in SPEC_NATURES
    if cfg.require_specification and needs_spec:
        if not r.specification.strip() and not all(line.specification.strip() for line in lines):
            raise ProcurementError(
                "A specification is required for non-consumable and asset requests.",
                code="specification_required",
                field="specification",
            )
    if r.raised_as_role in (R.OIC, R.LAB_OPERATOR) and not r.equipment_id:
        raise ProcurementError("Choose the equipment this request is for.", code="equipment_required")
