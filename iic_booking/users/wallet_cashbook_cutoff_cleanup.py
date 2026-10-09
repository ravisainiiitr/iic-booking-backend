"""
One-off report / cleanup for the cash-book matching cutoff (portal launch).

'Entry received' flags are computed from stored cash-book entries, so entries dated before the cutoff
stop flagging requests as soon as the cutoff is in force; this reports how many requests that clears.
A stored link to a pre-cutoff receipt is cleared only on requests that are not approved (approved /
credited requests are never touched). Output is counts and request ids only.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from django.db import transaction
from django.db.models import Q

from iic_booking.users.models.wallet import (
    WalletRechargeParseEntry,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.models.wallet_sric_settings import cashbook_match_from_date
from iic_booking.users.wallet_recharge_import import CashbookIndex

EARLIEST = date(1900, 1, 1)


def _open_requests():
    return (
        WalletRechargeRequest.objects.filter(
            is_deleted=False,
            cashbook_receipt_no="",
            status__in=[WalletRechargeRequestStatus.PENDING, WalletRechargeRequestStatus.APPROVED],
        )
        .select_related("department")
        .order_by("pk")
    )


def _precutoff_links(cutoff: date):
    return (
        WalletRechargeRequest.objects.exclude(cashbook_receipt_no="")
        .filter(Q(cashbook_receipt_date__lt=cutoff) | Q(cashbook_receipt_date__isnull=True))
        .order_by("pk")
    )


def run(*, apply: bool = False) -> dict[str, Any]:
    cutoff = cashbook_match_from_date()
    entries = WalletRechargeParseEntry.objects.all()
    report: dict[str, Any] = {
        "cutoff": cutoff.isoformat(),
        "entries_total": entries.count(),
        "entries_before_cutoff": entries.filter(dated__lt=cutoff).count(),
        "entries_undated": entries.filter(dated__isnull=True).count(),
    }

    everything = CashbookIndex(cutoff=EARLIEST)
    current = CashbookIndex(cutoff=cutoff)
    cleared = Counter()
    cleared_ids: dict[str, list[int]] = {}
    still = Counter()
    for req in _open_requests():
        before = bool(everything.candidates_for(req))
        now = bool(current.candidates_for(req))
        if now:
            still[req.status] += 1
        if before and not now:
            cleared[req.status] += 1
            cleared_ids.setdefault(req.status, []).append(req.pk)
    report["entry_received_flags_cleared_by_status"] = dict(cleared)
    report["entry_received_flags_cleared_ids"] = cleared_ids
    report["entry_received_flags_remaining_by_status"] = dict(still)

    links = list(_precutoff_links(cutoff))
    approved = [r.pk for r in links if r.status == WalletRechargeRequestStatus.APPROVED]
    unapproved = [r for r in links if r.status != WalletRechargeRequestStatus.APPROVED]
    report["precutoff_links_on_approved_untouched"] = len(approved)
    report["precutoff_links_on_approved_ids"] = approved
    report["precutoff_links_on_unapproved"] = len(unapproved)
    report["precutoff_links_on_unapproved_ids"] = [r.pk for r in unapproved]

    cleared_links = 0
    if apply and unapproved:
        from iic_booking.users.wallet_recharge_workflow import append_audit_log

        with transaction.atomic():
            for req in WalletRechargeRequest.objects.select_for_update().filter(pk__in=[r.pk for r in unapproved]):
                if req.status == WalletRechargeRequestStatus.APPROVED or not req.cashbook_receipt_no:
                    continue
                receipt = req.cashbook_receipt_no
                fields = ["cashbook_parse_entry", "cashbook_receipt_no", "cashbook_receipt_date", "cashbook_matched_at", "updated_at"]
                req.cashbook_parse_entry = None
                req.cashbook_receipt_no = ""
                req.cashbook_receipt_date = None
                req.cashbook_matched_at = None
                if (req.fund_receipt_verification_remarks or "").startswith("Matched SRIC cash-book receipt"):
                    req.fund_receipt_verified = False
                    req.fund_receipt_verified_at = None
                    req.fund_receipt_verified_by = None
                    req.fund_receipt_verification_remarks = ""
                    fields += [
                        "fund_receipt_verified",
                        "fund_receipt_verified_at",
                        "fund_receipt_verified_by",
                        "fund_receipt_verification_remarks",
                    ]
                req.save(update_fields=fields)
                append_audit_log(
                    req,
                    action="cashbook_link_cleared",
                    from_status=req.status,
                    to_status=req.status,
                    actor_email="cashbook-cutoff-cleanup",
                    message=f"Cash-book receipt {receipt} is dated before {cutoff:%d-%m-%Y}; link cleared.",
                    metadata={"receipt_no": receipt, "cutoff": cutoff.isoformat()},
                )
                cleared_links += 1
    report["mode"] = "APPLY" if apply else "DRY RUN"
    report["links_cleared_now"] = cleared_links
    return report
