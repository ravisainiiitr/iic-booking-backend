"""Item / vendor / GST masters and the configurable categories and request types.

Categories and request types decide approval routing (HOD rules, small-purchase eligibility), so only the Main
Administrator edits them. Items, vendors and GST rates need the ``masters`` permission (Office / OC Stores).
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import audit
from . import constants as c
from .api import choice, parse_bool, parse_qty, parse_rate, req_str
from .errors import ProcurementError, forbidden, not_found
from .models import GSTRate, Item, ItemCategory, RequestTypeConfig, Vendor
from .numbering import next_number

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum_ok(gstin: str) -> bool:
    total = 0
    for i, ch in enumerate(gstin[:14]):
        v = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return GSTIN_CHARS[(36 - total % 36) % 36] == gstin[14]


def clean_gstin(raw) -> str:
    value = str(raw or "").strip().upper()
    if not value:
        return ""
    if not GSTIN_RE.match(value) or not gstin_checksum_ok(value):
        raise ProcurementError("GSTIN is not valid.", code="invalid_gstin", field="gstin")
    return value


def clean_pan(raw) -> str:
    value = str(raw or "").strip().upper()
    if not value:
        return ""
    if not PAN_RE.match(value):
        raise ProcurementError("PAN is not valid.", code="invalid_pan", field="pan")
    return value


def _require_admin(scope):
    if not scope.admin:
        raise forbidden("Only the Main Administrator can change categories and request types.")


def _require_masters(scope, dept_id):
    scope.require_perm(dept_id, c.OfficePermission.MASTERS)


# ---------------------------------------------------------------------------
# Categories / request types (Main Admin)
# ---------------------------------------------------------------------------
CATEGORY_BOOLS = ("is_asset", "tracks_stock", "small_purchase_allowed", "approval_exempt", "hod_required_always", "active")
REQUEST_TYPE_BOOLS = (
    "requires_oic",
    "requires_stores",
    "stores_issue_flow",
    "allow_small_purchase",
    "requires_specification",
    "active",
)


@transaction.atomic
def save_category(scope, department, data: dict, *, instance: ItemCategory | None = None, request=None) -> ItemCategory:
    _require_admin(scope)
    fields = ("code", "name", "nature", *CATEGORY_BOOLS)
    before = audit.snapshot(instance, fields) if instance else {}
    cat = instance or ItemCategory(department=department)
    if instance is None:
        code = req_str(data, "code", max_len=40).upper()
        if ItemCategory.objects.filter(department=department, code=code).exists():
            raise ProcurementError("A category with this code already exists.", code="duplicate", field="code")
        cat.code = code
    if instance is None or "name" in data:
        cat.name = req_str(data, "name", max_len=120)
    if instance is None or "nature" in data:
        cat.nature = choice(data.get("nature"), c.ItemNature.values, "nature")
    for f in CATEGORY_BOOLS:
        if f in data:
            setattr(cat, f, parse_bool(data[f]))
    if instance is None and "is_asset" not in data:
        cat.is_asset = cat.nature in c.ASSET_NATURES
    if instance is None and "tracks_stock" not in data:
        cat.tracks_stock = cat.nature in c.STOCK_NATURES
    cat.save()
    old, new = audit.diff(before, audit.snapshot(cat, fields))
    audit.record(scope.user, "category.created" if instance is None else "category.updated", cat, old=old, new=new, request=request)
    return cat


@transaction.atomic
def update_request_type(scope, rt: RequestTypeConfig, data: dict, *, request=None) -> RequestTypeConfig:
    _require_admin(scope)
    fields = ("name", "default_nature", "hod_rule", "procurement_steps", *REQUEST_TYPE_BOOLS)
    before = audit.snapshot(rt, fields)
    if "name" in data:
        rt.name = req_str(data, "name", max_len=120)
    if "default_nature" in data:
        rt.default_nature = "" if not data["default_nature"] else choice(data["default_nature"], c.ItemNature.values, "default_nature")
    if "hod_rule" in data:
        rt.hod_rule = choice(data["hod_rule"], c.HodRule.values, "hod_rule")
    for f in REQUEST_TYPE_BOOLS:
        if f in data:
            setattr(rt, f, parse_bool(data[f]))
    if "procurement_steps" in data:
        steps = data["procurement_steps"] or []
        if not isinstance(steps, list) or any(s not in c.PROCUREMENT_STEP_ORDER for s in steps):
            raise ProcurementError("Unknown procurement step.", code="invalid_choice", field="procurement_steps")
        rt.procurement_steps = [s for s in c.PROCUREMENT_STEP_ORDER if s in steps]
    rt.save()
    old, new = audit.diff(before, audit.snapshot(rt, fields))
    audit.record(scope.user, "request_type.updated", rt, old=old, new=new, request=request)
    return rt


# ---------------------------------------------------------------------------
# GST rates
# ---------------------------------------------------------------------------
@transaction.atomic
def save_gst_rate(scope, department, data: dict, *, instance: GSTRate | None = None, request=None) -> GSTRate:
    _require_masters(scope, department.pk)
    fields = ("name", "rate", "cgst_rate", "sgst_rate", "igst_rate", "active")
    before = audit.snapshot(instance, fields) if instance else {}
    row = instance or GSTRate(department=department)
    if instance is None:
        rate = parse_rate(data.get("rate"), "rate")
        if GSTRate.objects.filter(department=department, rate=rate).exists():
            raise ProcurementError("This GST rate already exists.", code="duplicate", field="rate")
        row.rate = rate
        half = (rate / 2).quantize(Decimal("0.01"))
        row.cgst_rate, row.sgst_rate, row.igst_rate = half, half, rate
        row.name = f"GST {rate.normalize():f}%"
    if "name" in data:
        row.name = req_str(data, "name", max_len=60)
    for f in ("cgst_rate", "sgst_rate", "igst_rate"):
        if f in data:
            setattr(row, f, parse_rate(data[f], f))
    if row.cgst_rate + row.sgst_rate != row.rate or row.igst_rate != row.rate:
        raise ProcurementError("CGST + SGST and IGST must each equal the GST rate.", code="invalid_split")
    if "active" in data:
        row.active = parse_bool(data["active"])
    row.save()
    old, new = audit.diff(before, audit.snapshot(row, fields))
    audit.record(scope.user, "gst_rate.created" if instance is None else "gst_rate.updated", row, old=old, new=new, request=request)
    return row


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
VENDOR_FIELDS = ("name", "gstin", "pan", "address", "state", "contact_person", "phone", "email", "remarks", "active")


@transaction.atomic
def save_vendor(scope, department, data: dict, *, instance: Vendor | None = None, request=None) -> Vendor:
    _require_masters(scope, department.pk)
    if instance is not None and instance.is_archived:
        raise ProcurementError("Archived vendors cannot be edited.", code="archived")
    before = audit.snapshot(instance, VENDOR_FIELDS) if instance else {}
    v = instance or Vendor(department=department, created_by=scope.user)
    if instance is None or "name" in data:
        v.name = req_str(data, "name")
    if "gstin" in data or instance is None:
        v.gstin = clean_gstin(data.get("gstin"))
    if "pan" in data or instance is None:
        v.pan = clean_pan(data.get("pan"))
    if v.gstin and not v.pan:
        v.pan = v.gstin[2:12]
    if v.gstin and v.pan and v.gstin[2:12] != v.pan:
        raise ProcurementError("PAN does not match the GSTIN.", code="pan_gstin_mismatch", field="pan")
    for f, max_len in (("address", 2000), ("state", 60), ("contact_person", 120), ("phone", 40), ("email", 254), ("remarks", 2000)):
        if f in data:
            setattr(v, f, req_str(data, f, max_len=max_len, required=False))
    if "active" in data:
        v.active = parse_bool(data["active"])
    if v.gstin:
        dup = Vendor.objects.filter(department=department, gstin=v.gstin, is_archived=False)
        if v.pk:
            dup = dup.exclude(pk=v.pk)
        if dup.exists():
            raise ProcurementError("A vendor with this GSTIN already exists.", code="duplicate", field="gstin")
    if v.email:
        from django.core.validators import validate_email

        validate_email(v.email)
    if instance is None:
        v.code = next_number(c.NumberPrefix.VENDOR)
    v.save()
    old, new = audit.diff(before, audit.snapshot(v, VENDOR_FIELDS))
    audit.record(scope.user, "vendor.created" if instance is None else "vendor.updated", v, old=old, new=new, request=request)
    return v


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
ITEM_FIELDS = ("name", "category", "uom", "specification", "hsn_sac", "default_gst_rate", "min_level", "reorder_level", "active")


@transaction.atomic
def save_item(scope, department, data: dict, *, instance: Item | None = None, request=None) -> Item:
    _require_masters(scope, department.pk)
    if instance is not None and instance.is_archived:
        raise ProcurementError("Archived items cannot be edited.", code="archived")
    before = audit.snapshot(instance, ITEM_FIELDS) if instance else {}
    it = instance or Item(department=department, created_by=scope.user)
    if instance is None or "name" in data:
        it.name = req_str(data, "name")
    if instance is None or "category_id" in data:
        cat = ItemCategory.objects.filter(pk=data.get("category_id"), department=department, active=True).first()
        if cat is None:
            raise ProcurementError("Choose an active category of this department.", code="invalid_category", field="category_id")
        it.category = cat
    if "uom" in data:
        it.uom = req_str(data, "uom", max_len=30)
    for f, max_len in (("specification", 10000), ("hsn_sac", 20)):
        if f in data:
            setattr(it, f, req_str(data, f, max_len=max_len, required=False))
    if it.hsn_sac and not re.fullmatch(r"\d{4,8}", it.hsn_sac):
        raise ProcurementError("HSN / SAC must be 4 to 8 digits.", code="invalid_hsn", field="hsn_sac")
    if "default_gst_rate_id" in data:
        gid = data.get("default_gst_rate_id")
        if gid in (None, ""):
            it.default_gst_rate = None
        else:
            gst = GSTRate.objects.filter(pk=gid, department=department).first()
            if gst is None:
                raise ProcurementError("Unknown GST rate.", code="invalid_gst_rate", field="default_gst_rate_id")
            it.default_gst_rate = gst
    for f in ("min_level", "reorder_level"):
        if f in data:
            raw = data.get(f)
            setattr(it, f, Decimal("0.000") if raw in (None, "", 0, "0") else parse_qty(raw, f))
    if "active" in data:
        it.active = parse_bool(data["active"])
    if instance is None:
        it.code = next_number(c.NumberPrefix.ITEM)
    it.save()
    old, new = audit.diff(before, audit.snapshot(it, ITEM_FIELDS))
    audit.record(scope.user, "item.created" if instance is None else "item.updated", it, old=old, new=new, request=request)
    return it


@transaction.atomic
def archive(scope, obj, reason: str, *, request=None):
    """Soft delete for vendors and items (never hard-deleted)."""
    _require_masters(scope, obj.department_id)
    if obj.is_archived:
        raise ProcurementError("Already archived.", code="archived")
    obj.is_archived = True
    obj.active = False
    obj.archived_at = timezone.now()
    obj.archived_by = scope.user
    obj.archive_reason = reason
    obj.save(update_fields=["is_archived", "active", "archived_at", "archived_by", "archive_reason", "updated_at"])
    audit.record(scope.user, f"{type(obj).__name__.lower()}.archived", obj, reason=reason, request=request)
    return obj


def search_q(term: str, *fields: str) -> Q:
    q = Q()
    for f in fields:
        q |= Q(**{f"{f}__icontains": term})
    return q


def get_in_department(model, pk, dept_ids):
    obj = model.objects.filter(pk=pk, department_id__in=dept_ids).first()
    if obj is None:
        raise not_found()
    return obj
