"""Annual physical verification of assets (GFR Rule 213).

A campaign scopes a drive (optionally one register or laboratory). Each sighting is an append-only
``AssetVerification`` — scanned from the QR label, entered manually or imported — and updates the asset's
``last_verified_on`` / ``last_verification_result``. Shortages and damage show up in the campaign report.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, req_str
from .errors import ProcurementError, forbidden, not_found
from .fy import fy_label
from .models import Asset, AssetVerification, VerificationCampaign
from .numbering import next_number

P = c.OfficePermission
VR = c.VerificationResult
LIVE = ~Q(status__in=list(c.ASSET_FINAL_STATUSES))


def campaigns_qs():
    return VerificationCampaign.objects.select_related("department", "register", "laboratory", "created_by", "closed_by")


def campaign_assets(cmp: VerificationCampaign):
    qs = Asset.objects.filter(department_id=cmp.department_id, is_archived=False).filter(LIVE)
    if cmp.register_id:
        qs = qs.filter(register_id=cmp.register_id)
    if cmp.laboratory_id:
        qs = qs.filter(laboratory_id=cmp.laboratory_id)
    return qs


def campaign_stats(cmp: VerificationCampaign) -> dict:
    total = campaign_assets(cmp).count()
    rows = (
        AssetVerification.objects.filter(campaign=cmp)
        .order_by("asset_id", "-verified_on", "-id")
        .distinct("asset_id")
        .values_list("asset_id", "result")
    )
    by_result: dict[str, int] = {}
    for _, result in rows:
        by_result[result] = by_result.get(result, 0) + 1
    seen = sum(by_result.values())
    return {"total": total, "verified": seen, "pending": max(total - seen, 0), "by_result": by_result}


@transaction.atomic
def create_campaign(scope, data: dict, *, request=None) -> VerificationCampaign:
    from .assets import _lab_in
    from .registers import _cfg, get_register

    dept = access.pick_department(scope, data.get("department_id"))
    _cfg(dept.pk)
    scope.require_perm(dept.pk, P.ASSETS)
    started = parse_day(data.get("started_on"), "started_on") or timezone.localdate()
    cmp = VerificationCampaign.objects.create(
        number=next_number(c.NumberPrefix.VERIFICATION),
        department=dept,
        title=req_str(data, "title", required=False) or f"Annual physical verification {fy_label(started)}",
        financial_year=req_str(data, "financial_year", max_len=7, required=False) or fy_label(started),
        register=get_register(dept.pk, data.get("register_id")),
        laboratory=_lab_in(data.get("laboratory_id"), dept.pk),
        committee=req_str(data, "committee", max_len=5000, required=False),
        started_on=started,
        remarks=req_str(data, "remarks", max_len=5000, required=False),
        created_by=scope.user,
    )
    audit.record(scope.user, "verification.campaign_opened", cmp, new={"title": cmp.title}, request=request)
    return cmp


@transaction.atomic
def close_campaign(scope, cmp: VerificationCampaign, data: dict, *, request=None) -> VerificationCampaign:
    cmp = VerificationCampaign.objects.select_for_update().get(pk=cmp.pk)
    scope.require_perm(cmp.department_id, P.ASSETS)
    if cmp.status != c.CampaignStatus.OPEN:
        raise ProcurementError("The campaign is already closed.", code="invalid_status")
    cmp.status = c.CampaignStatus.CLOSED
    cmp.closed_on = timezone.localdate()
    cmp.closed_by = scope.user
    note = req_str(data, "remarks", max_len=5000, required=False)
    if note:
        cmp.remarks = (cmp.remarks + "\n" + note).strip()
    cmp.save()
    audit.record(scope.user, "verification.campaign_closed", cmp, new={"stats": campaign_stats(cmp)}, reason=note, request=request)
    return cmp


def can_verify(scope, asset: Asset) -> bool:
    return (
        scope.has_perm(asset.department_id, P.ASSETS)
        or scope.has_role(asset.department_id, c.ModuleRole.OC_STORES)
        or scope.has_role(asset.department_id, c.ModuleRole.AUDITOR)
        or scope.is_lab_staff_for(asset.equipment_id)
    )


@transaction.atomic
def verify(scope, asset: Asset, data: dict, *, request=None) -> AssetVerification:
    from .registers import _cfg

    _cfg(asset.department_id)
    if not can_verify(scope, asset):
        raise forbidden("You cannot verify this asset.")
    asset = Asset.objects.select_for_update(of=("self",)).get(pk=asset.pk)
    if asset.status in c.ASSET_FINAL_STATUSES:
        raise ProcurementError("Disposed or retired assets are not verified.", code="asset_final")
    cmp = None
    cid = parse_int(data.get("campaign_id"), "campaign_id")
    if cid:
        cmp = VerificationCampaign.objects.filter(pk=cid, department_id=asset.department_id).first()
        if cmp is None:
            raise not_found("Campaign not found.")
        if cmp.status != c.CampaignStatus.OPEN:
            raise ProcurementError("The campaign is closed.", code="invalid_status")
    result = choice(str(data.get("result") or "").upper(), VR.values, "result", default=VR.FOUND)
    verified_on = parse_day(data.get("verified_on"), "verified_on") or timezone.localdate()
    if verified_on > timezone.localdate():
        raise ProcurementError("The verification date cannot be in the future.", code="future_date", field="verified_on")
    qty_found = parse_int(data.get("quantity_found"), "quantity_found")
    if result == VR.SHORTAGE and (qty_found is None or qty_found >= asset.quantity):
        raise ProcurementError("Give the quantity actually found.", code="required", field="quantity_found")
    remarks = req_str(data, "remarks", max_len=5000, required=result != VR.FOUND)
    condition = choice(data.get("condition"), [""] + list(c.AssetCondition.values), "condition", default="")
    row = AssetVerification.objects.create(
        department_id=asset.department_id,
        asset=asset,
        campaign=cmp,
        verified_on=verified_on,
        verified_by=scope.user,
        result=result,
        condition=condition,
        quantity_found=qty_found,
        location_seen=req_str(data, "location_seen", required=False),
        remarks=remarks,
        method=choice(data.get("method"), c.VerificationMethod.values, "method", default=c.VerificationMethod.MANUAL),
    )
    asset.last_verified_on = verified_on
    asset.last_verification_result = result
    fields = ["last_verified_on", "last_verification_result", "updated_at"]
    if condition:
        asset.condition = condition
        fields.append("condition")
    asset.save(update_fields=fields)
    audit.record(
        scope.user, "asset.verified", asset, new={"result": result, "campaign": getattr(cmp, "number", None), "method": row.method},
        reason=remarks, request=request,
    )
    if result in (VR.NOT_FOUND, VR.SHORTAGE, VR.FOUND_DAMAGED):
        from . import notify

        notify.notify(
            notify.office_users(asset.department_id, P.ASSETS) + notify.department_role_users(asset.department_id, c.ModuleRole.OC_STORES),
            department_id=asset.department_id,
            title=f"Verification discrepancy: {asset.number}",
            message=f"{asset.description} ({asset.register_ref or asset.asset_tag}): {row.get_result_display()}. {remarks}",
            link=f"/procurement/assets/{asset.pk}",
            event="asset_verification",
            actor=scope.user,
            extra={"asset_id": asset.pk},
        )
    return row


def pending_assets(cmp: VerificationCampaign):
    done = AssetVerification.objects.filter(campaign=cmp).values("asset_id")
    return campaign_assets(cmp).exclude(pk__in=done)


def department_summary(dept_ids, fy: str) -> dict:
    """Assets verified in the given FY versus live assets — for dashboards."""
    from .fy import fy_bounds

    start, end = fy_bounds(fy)
    live = Asset.objects.filter(department_id__in=dept_ids, is_archived=False).filter(LIVE)
    total = live.count()
    verified = live.filter(last_verified_on__gte=start, last_verified_on__lte=end).count()
    issues = live.filter(
        last_verified_on__gte=start, last_verified_on__lte=end,
        last_verification_result__in=[VR.NOT_FOUND, VR.SHORTAGE, VR.FOUND_DAMAGED],
    ).count()
    open_campaigns = VerificationCampaign.objects.filter(department_id__in=dept_ids, status=c.CampaignStatus.OPEN).aggregate(n=Count("id"))["n"]
    return {"total": total, "verified": verified, "pending": total - verified, "discrepancies": issues, "open_campaigns": open_campaigns}
