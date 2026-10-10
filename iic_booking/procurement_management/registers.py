"""Physical asset registers (GFR Rule 211/212 — Major / Minor / dead-stock registers, Form 22).

* ``AssetRegister`` is one register book (code, type, volume). An asset's place in the book is
  (register, page, serial) and must be unique among live assets.
* Every asset gets a stable asset tag ``<DEPT>/<MAJ|MIN|LLA|...>/<000123>`` (printed on the QR label) unless one
  was already written on the item.
* Condemnation / write-off / disposal are recorded as append-only ``AssetDisposal`` rows with the board and
  sanction references (GFR Rules 214-217) and move the asset status.
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q

from . import access, audit
from . import constants as c
from .api import choice, parse_bool, parse_day, parse_int, parse_money, req_str
from .errors import ProcurementError, not_found
from .models import Asset, AssetDisposal, AssetRegister
from .numbering import next_number

P = c.OfficePermission
RT = c.RegisterType
NATURE_REGISTER = {v: k for k, v in c.REGISTER_CATEGORY_NATURE.items()}
NATURE_TAG = {
    c.ItemNature.MAJOR_ASSET: "MAJ",
    c.ItemNature.MINOR_ASSET: "MIN",
    c.ItemNature.LIMITED_LIFE_ASSET: "LLA",
}
REGISTER_FIELDS = ("register_type", "code", "name", "volume", "laboratory", "custodian", "opened_on", "closed_on",
                   "total_pages", "remarks", "active")
EXTRA_TEXT = (("supplier_name", 255), ("po_number", 80), ("invoice_number", 80), ("funding_source", 255),
              ("project_code", 80), ("legacy_ref", 120))
EXTRA_DATES = ("po_date", "invoice_date", "installation_date", "amc_until", "register_entry_date")
ENTRY_REF = re.compile(
    r"^\s*(?P<code>[A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:[/,:]|\s)\s*(?:p(?:age)?\.?\s*)?(?P<page>\d+)"
    r"(?:\s*(?:[/,:]|\s)\s*(?:s(?:l|no|r)?\.?\s*)?(?P<serial>[A-Za-z0-9\-]+))?\s*$",
    re.IGNORECASE,
)


def _cfg(dept_id):
    cfg = access.require_config(dept_id)
    if not cfg.asset_register_enabled:
        raise ProcurementError("The asset register is not enabled for this department.", code="feature_disabled")
    return cfg


# ---------------------------------------------------------------------------
# Register books
# ---------------------------------------------------------------------------
def registers_qs():
    return AssetRegister.objects.select_related("department", "laboratory", "custodian").filter(is_archived=False)


def with_entry_counts(qs):
    return qs.annotate(entry_total=Count("entries", filter=Q(entries__is_archived=False)))


@transaction.atomic
def save_register(scope, data: dict, reg: AssetRegister | None = None, *, request=None) -> AssetRegister:
    from .assets import _lab_in, _user

    creating = reg is None
    if creating:
        dept = access.pick_department(scope, data.get("department_id"))
        reg = AssetRegister(department=dept, created_by=scope.user)
    else:
        reg = AssetRegister.objects.select_for_update().get(pk=reg.pk)
    _cfg(reg.department_id)
    scope.require_perm(reg.department_id, P.ASSETS)
    before = {} if creating else audit.snapshot(reg, REGISTER_FIELDS)
    if creating or "register_type" in data:
        reg.register_type = choice(data.get("register_type"), RT.values, "register_type")
    if creating or "code" in data:
        code = req_str(data, "code", max_len=40).upper().replace(" ", "-")
        if AssetRegister.objects.filter(department_id=reg.department_id, code__iexact=code).exclude(pk=reg.pk).exists():
            raise ProcurementError(f"A register with code {code} already exists.", code="duplicate_register", field="code")
        reg.code = code
    if creating or "name" in data:
        reg.name = req_str(data, "name", required=False) or f"{RT(reg.register_type).label} register"
    if "volume" in data:
        reg.volume = req_str(data, "volume", max_len=40, required=False)
    if "laboratory_id" in data:
        reg.laboratory = _lab_in(data.get("laboratory_id"), reg.department_id)
    if "custodian_id" in data:
        reg.custodian = _user(data.get("custodian_id"), "custodian_id")
    for f in ("opened_on", "closed_on"):
        if f in data:
            setattr(reg, f, parse_day(data.get(f), f))
    if "total_pages" in data:
        pages = parse_int(data.get("total_pages"), "total_pages")
        if pages is not None and pages < 1:
            raise ProcurementError("total_pages must be at least 1.", code="invalid", field="total_pages")
        if pages and not creating:
            used = reg.entries.filter(is_archived=False).aggregate(m=Max("register_page"))["m"] or 0
            if used > pages:
                raise ProcurementError(f"Entries already use page {used}.", code="invalid", field="total_pages")
        reg.total_pages = pages
    if "remarks" in data:
        reg.remarks = req_str(data, "remarks", max_len=5000, required=False)
    if "active" in data:
        reg.active = parse_bool(data.get("active"))
    reg.save()
    if creating:
        audit.record(scope.user, "register.created", reg, new=audit.snapshot(reg, REGISTER_FIELDS), request=request)
    else:
        old, new = audit.diff(before, audit.snapshot(reg, REGISTER_FIELDS))
        if new:
            audit.record(scope.user, "register.updated", reg, old=old, new=new, request=request)
    return reg


def get_register(dept_id, raw, field="register_id") -> AssetRegister | None:
    rid = parse_int(raw, field)
    if not rid:
        return None
    reg = AssetRegister.objects.filter(pk=rid, department_id=dept_id, is_archived=False).first()
    if reg is None:
        raise ProcurementError("Register not found in this department.", code="invalid_register", field=field)
    return reg


# ---------------------------------------------------------------------------
# Entries on assets
# ---------------------------------------------------------------------------
def duplicate_entry(register_id, page, serial, *, exclude_pk=None) -> Asset | None:
    if not register_id or not page or not serial:
        return None
    qs = Asset.objects.filter(
        register_id=register_id, register_page=page, register_serial__iexact=serial, is_archived=False
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


def _page(raw, reg, field="register_page"):
    page = parse_int(raw, field)
    if page is None:
        return None
    if page < 1:
        raise ProcurementError("The page number must be 1 or more.", code="invalid", field=field)
    if reg is not None and reg.total_pages and page > reg.total_pages:
        raise ProcurementError(f"{reg.code} has only {reg.total_pages} pages.", code="invalid", field=field)
    return page


def _increment(serial: str, step: int) -> str:
    m = re.match(r"^(.*?)(\d+)$", serial or "")
    if not m:
        return ""
    head, num = m.groups()
    return f"{head}{int(num) + step:0{len(num)}d}"


def next_free_entry(reg: AssetRegister) -> tuple[int, str]:
    """Last page in use and the next serial on it (used to auto-place capital items received through procurement)."""
    last = (
        Asset.objects.filter(register=reg, is_archived=False, register_page__isnull=False)
        .order_by("-register_page", "-id")
        .values("register_page", "register_serial")
        .first()
    )
    if not last:
        return 1, "1"
    page = last["register_page"]
    serials = Asset.objects.filter(register=reg, register_page=page, is_archived=False).values_list("register_serial", flat=True)
    nums = [int(s) for s in serials if str(s).isdigit()]
    return page, str((max(nums) + 1) if nums else 1)


def default_register(dept_id, nature) -> AssetRegister | None:
    rtype = NATURE_REGISTER.get(nature)
    if not rtype:
        return None
    regs = list(AssetRegister.objects.filter(department_id=dept_id, register_type=rtype, active=True, is_archived=False)[:2])
    return regs[0] if len(regs) == 1 else None


def entry_values(data: dict, dept_id: int, count: int, *, category=None, auto_place: bool = False) -> list[dict]:
    """Register placement for ``count`` new assets: explicit (register, page, serial[s]) or, when ``auto_place`` and
    the department keeps exactly one active register of the matching type, the next free serial."""
    reg = get_register(dept_id, data.get("register_id"))
    if reg is None and data.get("register_code"):
        reg = AssetRegister.objects.filter(department_id=dept_id, code__iexact=str(data["register_code"]).strip(), is_archived=False).first()
        if reg is None:
            raise ProcurementError("Register not found in this department.", code="invalid_register", field="register_code")
    if reg is None and auto_place and category is not None:
        reg = default_register(dept_id, category.nature)
        if reg is not None and not data.get("register_page"):
            page, serial = next_free_entry(reg)
            return [{"register": reg, "register_page": page, "register_serial": _increment(serial, i) or serial} for i in range(count)]
    if reg is None:
        if data.get("register_page") or data.get("register_serial"):
            raise ProcurementError("Choose the register for the page / serial.", code="required", field="register_id")
        return [{} for _ in range(count)]
    if not reg.active:
        raise ProcurementError(f"Register {reg.code} is closed.", code="register_closed", field="register_id")
    page = _page(data.get("register_page"), reg)
    if page is None:
        raise ProcurementError("Give the register page number.", code="required", field="register_page")
    raw = data.get("register_serials")
    if isinstance(raw, list) and raw:
        if len(raw) != count:
            raise ProcurementError("Give one register serial per asset.", code="serial_count", field="register_serials")
        serials = [str(x or "").strip()[:20] for x in raw]
    else:
        first = str(data.get("register_serial") or "").strip()[:20]
        if count > 1 and first and not _increment(first, 1):
            raise ProcurementError(
                "For several assets give a numeric first serial (it is incremented) or one serial per asset.",
                code="serial_count", field="register_serial",
            )
        serials = [_increment(first, i) if i else first for i in range(count)] if first else [""] * count
    filled = [s.upper() for s in serials if s]
    if len(filled) != len(set(filled)):
        raise ProcurementError("Register serials must be unique.", code="duplicate_entry", field="register_serials")
    clashes = []
    for s in serials:
        hit = duplicate_entry(reg.pk, page, s)
        if hit is not None:
            clashes.append({"serial": s, "asset_id": hit.pk, "asset_number": hit.number})
    if clashes:
        raise ProcurementError(
            f"{reg.code} page {page} serial {', '.join(x['serial'] for x in clashes)} is already used.",
            code="duplicate_entry", field="register_serial", clashes=clashes,
        )
    entry_date = parse_day(data.get("register_entry_date"), "register_entry_date")
    return [
        {"register": reg, "register_page": page, "register_serial": s, **({"register_entry_date": entry_date} if entry_date else {})}
        for s in serials
    ]


def extra_values(data: dict, dept_id: int, *, partial: bool = False) -> dict:
    """GFR register columns beyond the original asset form."""
    out: dict = {}
    for f, limit in EXTRA_TEXT:
        if not partial or f in data:
            out[f] = req_str(data, f, max_len=limit, required=False)
    for f in EXTRA_DATES:
        if f == "register_entry_date":
            continue
        if not partial or f in data:
            out[f] = parse_day(data.get(f), f)
    if not partial or "condition" in data:
        out["condition"] = choice(data.get("condition"), [""] + list(c.AssetCondition.values), "condition", default="")
    if not partial or "quantity" in data:
        qty = parse_int(data.get("quantity"), "quantity") or 1
        if qty < 1:
            raise ProcurementError("quantity must be at least 1.", code="invalid", field="quantity")
        out["quantity"] = qty
    if not partial or "useful_life_years" in data:
        out["useful_life_years"] = parse_int(data.get("useful_life_years"), "useful_life_years")
    if not partial or "depreciation_rate" in data:
        out["depreciation_rate"] = parse_money(data.get("depreciation_rate"), "depreciation_rate", required=False)
    if not partial or "parent_id" in data:
        out["parent"] = parent_asset(dept_id, data.get("parent_id"))
    return out


def parent_asset(dept_id, raw, field="parent_id") -> Asset | None:
    pid = parse_int(raw, field)
    if not pid:
        return None
    parent = Asset.objects.filter(pk=pid, department_id=dept_id, is_archived=False).first()
    if parent is None:
        raise ProcurementError("Main asset not found in this department.", code="invalid_parent", field=field)
    if parent.parent_id:
        raise ProcurementError("Accessories cannot have their own accessories.", code="invalid_parent", field=field)
    return parent


def tag_for(asset: Asset) -> str:
    dept = asset.department
    prefix = (dept.code or f"D{dept.pk}").upper().replace(" ", "")
    kind = c.REGISTER_TAG_CODE.get(asset.register.register_type) if asset.register_id else None
    kind = kind or NATURE_TAG.get(asset.category.nature, "AST")
    return f"{prefix}/{kind}/{asset.pk:06d}"


def ensure_tag(asset: Asset) -> None:
    if not asset.asset_tag:
        asset.asset_tag = tag_for(asset)
        Asset.objects.filter(pk=asset.pk).update(asset_tag=asset.asset_tag)


def save_entry(asset: Asset, **fields) -> None:
    """Save with a friendly error when the (register, page, serial) constraint trips under a race."""
    try:
        with transaction.atomic():
            asset.save(**fields)
    except IntegrityError as exc:
        if "pm_asset_unique_register_entry" in str(exc):
            raise ProcurementError("This register page / serial is already used.", code="duplicate_entry")
        raise


def search_q(term: str) -> Q:
    """Free-text asset search; also understands register references like ``MAJ-1/12/3`` or ``MAJ-1 p12 s3``."""
    term = (term or "").strip()
    q = (
        Q(number__icontains=term) | Q(description__icontains=term) | Q(serial_number__icontains=term)
        | Q(asset_tag__icontains=term) | Q(make__icontains=term) | Q(model_number__icontains=term)
        | Q(legacy_ref__icontains=term) | Q(register__code__iexact=term) | Q(po_number__icontains=term)
        | Q(invoice_number__icontains=term)
    )
    m = ENTRY_REF.match(term)
    if m:
        ref = Q(register__code__iexact=m["code"], register_page=int(m["page"]))
        if m["serial"]:
            ref &= Q(register_serial__iexact=m["serial"])
        q |= ref
    return q


def lookup(scope, *, tag: str = "", register_id=None, page=None, serial: str = "") -> Asset | None:
    from .views_assets import _assets_qs, assets_q

    qs = _assets_qs().filter(assets_q(scope))
    if tag:
        tag = tag.strip()
        return qs.filter(Q(asset_tag__iexact=tag) | Q(number__iexact=tag)).first()
    if register_id and page:
        qs = qs.filter(register_id=register_id, register_page=page)
        if serial:
            qs = qs.filter(register_serial__iexact=serial)
        return qs.first()
    return None


# ---------------------------------------------------------------------------
# Condemnation / write-off / disposal
# ---------------------------------------------------------------------------
@transaction.atomic
def dispose(scope, asset: Asset, data: dict, *, request=None) -> AssetDisposal:
    from .assets import DISPOSE_FROM, OPEN_TRANSFER, _history, _lock

    asset = _lock(asset)
    _cfg(asset.department_id)
    scope.require_perm(asset.department_id, P.ASSETS)
    action = choice(str(data.get("action") or "").upper(), c.DisposalAction.values, "action")
    if asset.status in c.ASSET_FINAL_STATUSES:
        raise ProcurementError("This asset is already disposed or retired.", code="asset_final")
    if asset.status in c.ASSET_TRANSFER_STATUSES or asset.transfers.filter(status__in=OPEN_TRANSFER).exists():
        raise ProcurementError("Finish the open transfer first.", code="transfer_open")
    if action == c.DisposalAction.CONDEMN and asset.status == c.AssetStatus.CONDEMNED:
        raise ProcurementError("The asset is already condemned.", code="no_change")
    if action == c.DisposalAction.DISPOSE and asset.status not in DISPOSE_FROM:
        raise ProcurementError("Condemn the asset (or mark it damaged / lost) before disposal.", code="invalid_transition")
    if action == c.DisposalAction.WRITE_OFF and asset.status not in DISPOSE_FROM:
        raise ProcurementError("Only lost, damaged or condemned assets can be written off.", code="invalid_transition")
    board = req_str(data, "board_reference", required=action == c.DisposalAction.CONDEMN)
    sanction = req_str(data, "sanction_reference", required=action != c.DisposalAction.CONDEMN)
    remarks = req_str(data, "remarks", max_len=5000, required=True)
    to_status = c.DISPOSAL_STATUS[action]
    row = AssetDisposal.objects.create(
        number=next_number(c.NumberPrefix.DISPOSAL),
        department_id=asset.department_id,
        asset=asset,
        action=action,
        mode=choice(data.get("mode"), [""] + list(c.DisposalMode.values), "mode", default=""),
        board_reference=board,
        sanction_reference=sanction,
        sanction_date=parse_day(data.get("sanction_date"), "sanction_date"),
        book_value=parse_money(data.get("book_value"), "book_value", required=False),
        realised_value=parse_money(data.get("realised_value"), "realised_value", required=False),
        from_status=asset.status,
        to_status=to_status,
        remarks=remarks,
        recorded_by=scope.user,
    )
    from_status = asset.status
    asset.status = to_status
    if action != c.DisposalAction.CONDEMN:
        asset.condition = c.AssetCondition.UNSERVICEABLE if asset.condition == "" else asset.condition
    asset.save(update_fields=["status", "condition", "updated_at"])
    _history(asset, from_status, to_status, f"{row.get_action_display()} {row.number}: {remarks}", scope.user)
    audit.record(
        scope.user, f"asset.{action.lower()}", asset, old={"status": from_status},
        new={"status": to_status, "disposal": row.number, "board": board, "sanction": sanction}, reason=remarks, request=request,
    )
    return row


def get_visible_register(scope, pk) -> AssetRegister:
    reg = registers_qs().filter(pk=pk, department_id__in=scope.department_ids()).first()
    if reg is None:
        raise not_found("Register not found.")
    return reg
