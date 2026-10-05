"""Default categories, request types and GST rates for a department.

Seeded lazily (first time a department's masters are read or the module is enabled) instead of by data
migration, so deploying the app writes nothing to existing departments. Existing rows are never overwritten.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from . import constants as c
from .models import GSTRate, ItemCategory, RequestTypeConfig

N = c.ItemNature
H = c.HodRule
S = c.ProcurementStep

DEFAULT_CATEGORIES = [
    # code, name, nature, is_asset, tracks_stock, small_purchase_allowed, hod_required_always
    ("CONSUMABLE", "Consumable", N.CONSUMABLE, False, True, True, False),
    ("NON_CONSUMABLE", "Non-Consumable", N.NON_CONSUMABLE, False, False, True, False),
    ("LIMITED_LIFE_ASSET", "Limited-Life Asset", N.LIMITED_LIFE_ASSET, True, False, True, False),
    ("MINOR_ASSET", "Minor Asset", N.MINOR_ASSET, True, False, True, False),
    ("MAJOR_ASSET", "Major Asset", N.MAJOR_ASSET, True, False, False, True),
    ("AMC_SERVICE", "AMC / Service", N.AMC_SERVICE, False, False, True, False),
    ("GENERAL_OFFICE", "General Office", N.GENERAL_OFFICE, False, True, True, False),
]

FULL_STEPS = [s.value for s in S]
SERVICE_STEPS = [
    S.INDENT,
    S.SPECIFICATION,
    S.RFQ,
    S.QUOTATIONS,
    S.COMPARATIVE,
    S.VENDOR_SELECTION,
    S.PURCHASE_ORDER,
    S.INSPECTION,
    S.INVOICE,
    S.PAYMENT,
]

DEFAULT_REQUEST_TYPES = [
    # code, nature, requires_oic, requires_stores, hod_rule, stores_issue_flow, requires_spec, steps
    (c.RequestTypeCode.GENERAL_OFFICE, N.GENERAL_OFFICE, False, True, H.NEVER, True, False, FULL_STEPS),
    (c.RequestTypeCode.CONSUMABLE, N.CONSUMABLE, True, True, H.ABOVE_THRESHOLD, True, False, FULL_STEPS),
    (c.RequestTypeCode.NON_CONSUMABLE, N.NON_CONSUMABLE, True, True, H.ABOVE_THRESHOLD, False, True, FULL_STEPS),
    (c.RequestTypeCode.MINOR_ASSET, N.MINOR_ASSET, True, True, H.ABOVE_THRESHOLD, False, True, FULL_STEPS),
    (c.RequestTypeCode.MAJOR_ASSET, N.MAJOR_ASSET, True, True, H.ALWAYS, False, True, FULL_STEPS),
    (c.RequestTypeCode.LIMITED_LIFE_ASSET, N.LIMITED_LIFE_ASSET, True, True, H.ABOVE_THRESHOLD, False, True, FULL_STEPS),
    (c.RequestTypeCode.AMC, N.AMC_SERVICE, True, False, H.ABOVE_THRESHOLD, False, False, SERVICE_STEPS),
    (c.RequestTypeCode.REPAIR_MAINTENANCE, N.AMC_SERVICE, True, False, H.ABOVE_THRESHOLD, False, False, SERVICE_STEPS),
    (c.RequestTypeCode.SERVICE, N.AMC_SERVICE, True, False, H.ABOVE_THRESHOLD, False, False, SERVICE_STEPS),
    (c.RequestTypeCode.PLAN_GRANT, "", True, True, H.ALWAYS, False, True, FULL_STEPS),
    (c.RequestTypeCode.NON_PLAN, "", True, True, H.ABOVE_THRESHOLD, False, False, FULL_STEPS),
    (c.RequestTypeCode.OTHER, "", True, True, H.ABOVE_THRESHOLD, False, False, FULL_STEPS),
]

DEFAULT_GST_RATES = ["0", "5", "12", "18", "28", "40"]


@transaction.atomic
def ensure_department_defaults(department) -> None:
    if not ItemCategory.objects.filter(department=department).exists():
        ItemCategory.objects.bulk_create(
            [
                ItemCategory(
                    department=department,
                    code=code,
                    name=name,
                    nature=nature,
                    is_asset=is_asset,
                    tracks_stock=tracks,
                    small_purchase_allowed=small,
                    hod_required_always=hod,
                )
                for code, name, nature, is_asset, tracks, small, hod in DEFAULT_CATEGORIES
            ],
            ignore_conflicts=True,
        )
    if not RequestTypeConfig.objects.filter(department=department).exists():
        RequestTypeConfig.objects.bulk_create(
            [
                RequestTypeConfig(
                    department=department,
                    code=code,
                    name=str(code.label),
                    default_nature=nature,
                    requires_oic=oic,
                    requires_stores=stores,
                    hod_rule=hod,
                    stores_issue_flow=issue,
                    requires_specification=spec,
                    procurement_steps=[str(s) for s in steps],
                )
                for code, nature, oic, stores, hod, issue, spec, steps in DEFAULT_REQUEST_TYPES
            ],
            ignore_conflicts=True,
        )
    if not GSTRate.objects.filter(department=department).exists():
        rows = []
        for raw in DEFAULT_GST_RATES:
            rate = Decimal(raw)
            half = (rate / 2).quantize(Decimal("0.01"))
            rows.append(
                GSTRate(
                    department=department,
                    name=f"GST {raw}%",
                    rate=rate,
                    cgst_rate=half,
                    sgst_rate=half,
                    igst_rate=rate,
                )
            )
        GSTRate.objects.bulk_create(rows, ignore_conflicts=True)
