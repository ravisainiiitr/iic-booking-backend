"""Reverse a Project Grant wallet recharge that was approved only by its requester / wallet owner.

Such a request was credited without the SRIC Office approving it and without any SRIC cash-book receipt
or fund-receipt verification. The reversal is a Main Admin ledger debit (WalletAdminAdjustment, WAD-…)
of the full credited amount on the same sub-wallet; the request is then cancelled by the administrator
with an audit-log entry, and the requester / wallet owner are told how to recharge through the SRIC
Portal instead. Nothing is reversed partially: a request whose credit has been spent, or whose reversal
would take the balance below zero, is refused.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from html import escape
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.users.models import User, UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    WalletRechargeCancellationSource,
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)

logger = logging.getLogger(__name__)

REVERSAL_REASON = (
    "Reversed: project-grant recharge not confirmed by SRIC; please recharge via the new project-grant "
    "process (rnd.iitr.ac.in)"
)
AUDIT_ACTION = "reversed_not_confirmed_by_sric"
NOTICE_ACTION = "reversal_notice_sent"
TXN_RE = re.compile(r"^IIC-TXN-(\d{6})$")
# Any of these means SRIC, the cash-book or a finance check touched the request.
CONFIRMATION_ACTIONS = {
    "cashbook_matched",
    "fund_receipt_verified",
    "wallet_credited_on_fund_receipt",
    "approved_credit_deferred",
    "decline_credit_recovered",
}
ZERO = Decimal("0.00")


class ReversalError(Exception):
    pass


def client_request_id(recharge_request: WalletRechargeRequest) -> str:
    return f"recharge-reversal-{recharge_request.pk}"


def parse_txn(value: str) -> int:
    m = TXN_RE.fullmatch((value or "").strip().upper())
    if not m:
        raise ReversalError(f"'{value}' is not a transaction number like IIC-TXN-000049.")
    return int(m.group(1))


def _low(value) -> str:
    return (value or "").strip().lower()


def _parties(req: WalletRechargeRequest) -> tuple[set[int], set[str]]:
    owner = req.wallet.user
    ids = {req.user_id, owner.pk}
    emails = {_low(req.user.email), _low(owner.email)} - {""}
    return ids, emails


def approval_source(req: WalletRechargeRequest) -> str:
    """Who approved: sric_cashbook / sric_email_link / sric_approver / staff / user (requester or owner) / unknown."""
    from iic_booking.users.wallet_recharge_workflow import get_recharge_approver_emails

    approved_by = _low(req.approved_by_email)
    ids, emails = _parties(req)
    if approved_by.startswith("sric-cashbook") or (req.response_message or "").startswith(
        "Approved against SRIC cash-book receipt"
    ):
        return "sric_cashbook"
    if approved_by.startswith("sric-"):
        return "sric_email_link"
    if approved_by and approved_by in {_low(e) for e in get_recharge_approver_emails(req)}:
        return "sric_approver"
    if req.processed_by_id and req.processed_by_id not in ids:
        return "staff"
    if (req.processed_by_id and req.processed_by_id in ids) or (approved_by and approved_by in emails):
        return "user"
    return "unknown"


def _credit_entry(req: WalletRechargeRequest, sub: SubWallet) -> list[SubWalletTransaction]:
    ref = re.compile(rf"(WRR-{req.pk}(?!\d)|{re.escape(req.transaction_number)}(?!\d))", re.I)
    return [
        t
        for t in SubWalletTransaction.objects.filter(sub_wallet=sub, transaction_type="credit").order_by("created_at", "pk")
        if ref.search(t.description or "")
    ]


def assess(req: WalletRechargeRequest) -> dict[str, Any]:
    """Facts and the reasons (if any) this request must not be reversed. Read-only."""
    from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment

    blockers: list[str] = []
    ids, _ = _parties(req)
    existing = WalletAdminAdjustment.objects.filter(client_request_id=client_request_id(req)).first()
    facts: dict[str, Any] = {
        "txn": req.transaction_number,
        "request_id": req.pk,
        "status": req.status,
        "mode": req.recharge_mode,
        "amount": req.amount,
        "owner_id": req.wallet.user_id,
        "requester_id": req.user_id,
        "department_id": req.department_id,
        "approval_source": approval_source(req),
        "cashbook_matched": bool(req.cashbook_parse_entry_id or (req.cashbook_receipt_no or "").strip() or req.cashbook_matched_at),
        "fund_receipt_verified": bool(req.fund_receipt_verified),
        "wallet_credited_at": req.wallet_credited_at,
        "existing_reversal": existing.reference if existing else "",
        "already_reversed": req.audit_logs.filter(action=AUDIT_ACTION).exists(),
    }
    if facts["already_reversed"]:
        return {**facts, "blockers": [], "eligible": False}

    if req.status != WalletRechargeRequestStatus.APPROVED:
        blockers.append(f"status is {req.status}, not APPROVED")
    if req.recharge_mode != WalletRechargeMode.PROJECT_GRANT:
        blockers.append("not a Project Grant recharge")
    if req.is_deleted:
        blockers.append("request is deleted")
    if facts["approval_source"] != "user":
        blockers.append(f"approved by {facts['approval_source']}, not by the requester / wallet owner")
    if facts["cashbook_matched"]:
        blockers.append("SRIC cash-book receipt is linked")
    if facts["fund_receipt_verified"]:
        blockers.append("fund receipt is verified")
    if req.wallet_credit_pending or not req.wallet_credited_at:
        blockers.append("the wallet was not credited for this request")
    if (req.credit_settled_amount or ZERO) > 0:
        blockers.append("part of the credit settled an earlier credit")
    actions = list(req.audit_logs.values_list("action", "actor_id"))
    touched = sorted({a for a, _ in actions if a in CONFIRMATION_ACTIONS})
    if touched:
        blockers.append(f"audit log has confirmation steps {touched}")
    approvals = [actor for a, actor in actions if a == "approved"]
    if len(approvals) != 1 or (approvals[0] is not None and approvals[0] not in ids):
        blockers.append("the approval audit entry is not a single approval by the requester / wallet owner")

    sub = SubWallet.objects.filter(wallet_id=req.wallet_id, department_id=req.department_id).first()
    facts["sub_wallet_id"] = sub.pk if sub else None
    if sub is None:
        blockers.append("no sub-wallet for the request's department")
        return {**facts, "blockers": blockers, "eligible": False}
    credits = _credit_entry(req, sub)
    facts["credit_entry_ids"] = [c.pk for c in credits]
    if len(credits) != 1 or credits[0].amount != req.amount:
        blockers.append("could not find exactly one ledger credit of the request amount")
    else:
        credit = credits[0]
        facts["credited_on"] = credit.created_at
        later = SubWalletTransaction.objects.filter(sub_wallet=sub).filter(
            Q(created_at__gt=credit.created_at) | Q(created_at=credit.created_at, pk__gt=credit.pk)
        )
        facts["debits_since_credit"] = later.filter(transaction_type="debit").count()
        facts["credits_since_credit"] = later.filter(transaction_type="credit").count()
        if facts["debits_since_credit"]:
            blockers.append(f"{facts['debits_since_credit']} debit(s) on the sub-wallet since the credit (funds may be in use)")
    facts["balance_before"] = sub.balance
    facts["balance_after"] = sub.balance - req.amount
    if facts["balance_after"] < 0:
        blockers.append(f"reversal would take the balance to {facts['balance_after']}")
    if existing and not facts["already_reversed"]:
        blockers.append(f"a reversal debit {existing.reference} exists but the request is not marked; check manually")
    return {**facts, "blockers": blockers, "eligible": not blockers}


def main_admin(actor_id: int | None = None) -> User:
    qs = User.objects.filter(is_active=True, is_test_account=False).filter(Q(is_superuser=True) | Q(user_type=UserType.ADMIN))
    actor = qs.filter(pk=actor_id).first() if actor_id else qs.order_by("-is_superuser", "pk").first()
    if actor is None:
        raise ReversalError("No active Main Administrator account to act as.")
    return actor


def reverse(req: WalletRechargeRequest, *, actor: User) -> dict[str, Any]:
    """Debit the credit back out and cancel the request. Refuses unless assess() finds no blocker."""
    from iic_booking.users.admin_wallet_ledger import LedgerError, perform_adjustment
    from iic_booking.users.wallet_recharge_workflow import append_audit_log

    with transaction.atomic():
        locked = (
            WalletRechargeRequest.objects.select_for_update(of=("self",))
            .select_related("user", "wallet__user", "department")
            .get(pk=req.pk)
        )
        facts = assess(locked)
        if facts["already_reversed"]:
            return {**facts, "applied": False}
        if not facts["eligible"]:
            raise ReversalError(f"{locked.transaction_number}: not reversed — {'; '.join(facts['blockers'])}")
        previous = {
            "approved_by_role": facts["approval_source"],
            "processed_by_id": locked.processed_by_id,
            "responded_at": locked.responded_at.isoformat() if locked.responded_at else None,
            "wallet_credited_at": locked.wallet_credited_at.isoformat() if locked.wallet_credited_at else None,
        }
        try:
            record, _ = perform_adjustment(
                actor=actor,
                data={
                    "client_request_id": client_request_id(locked),
                    "owner_id": locked.wallet.user_id,
                    "sub_wallet_id": facts["sub_wallet_id"],
                    "direction": "debit",
                    "amount": f"{locked.amount:.2f}",
                    "reason": "correction",
                    "remarks": f"{REVERSAL_REASON} — {locked.transaction_number}",
                    "external_reference": locked.transaction_number,
                    "notify_owner": False,
                },
            )
        except LedgerError as exc:
            raise ReversalError(f"{locked.transaction_number}: ledger debit refused ({exc.code})") from exc
        now = timezone.now()
        locked.status = WalletRechargeRequestStatus.CANCELLED
        locked.cancellation_source = WalletRechargeCancellationSource.ADMIN
        locked.approved_by_email = actor.email or ""
        locked.processed_by = actor
        locked.response_message = REVERSAL_REASON
        locked.responded_at = now
        locked.save(
            update_fields=[
                "status",
                "cancellation_source",
                "approved_by_email",
                "processed_by",
                "response_message",
                "responded_at",
                "updated_at",
            ]
        )
        append_audit_log(
            locked,
            action=AUDIT_ACTION,
            from_status=WalletRechargeRequestStatus.APPROVED,
            to_status=WalletRechargeRequestStatus.CANCELLED,
            actor=actor,
            message=f"{REVERSAL_REASON} ({record.reference})",
            metadata={
                "adjustment_reference": record.reference,
                "adjustment_id": record.pk,
                "sub_wallet_transaction_id": record.sub_wallet_transaction_id,
                "amount": str(record.amount),
                "balance_before": str(record.balance_before),
                "balance_after": str(record.balance_after),
                "previous": previous,
            },
        )
    return {
        **facts,
        "applied": True,
        "adjustment_reference": record.reference,
        "adjustment_id": record.pk,
        "sub_wallet_transaction_id": record.sub_wallet_transaction_id,
        "balance_before": record.balance_before,
        "balance_after": record.balance_after,
    }


# --- Notice to the requester / wallet owner -------------------------------------------------------


def notice_recipients(req: WalletRechargeRequest) -> list[User]:
    out: list[User] = []
    for user in (req.wallet.user, req.user):
        if user.pk not in {u.pk for u in out} and (user.email or "").strip():
            out.append(user)
    return out


def _links() -> dict[str, str]:
    from django.conf import settings

    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.models.sric_wallet_recharge import SRIC_PORTAL_URL

    return {
        "wallet": get_frontend_absolute_url("/wallet"),
        "guide": get_frontend_absolute_url("/wallet/recharge-from-project"),
        "tickets": get_frontend_absolute_url("/tickets"),
        "sric": SRIC_PORTAL_URL,
        "support": (getattr(settings, "SUPPORT_EMAIL", "") or "").strip() or "iicbooking@iitr.ac.in",
    }


SRIC_STEPS = [
    "Sign in to the SRIC Portal at {sric}.",
    "In the left menu, open Ledger > New Wallet Recharge.",
    "Under Select Project, search by project number or title and select the project that will fund the recharge.",
    "Choose the Receiver Type: IIC for IIC instruments, or Tinkering for Tinkering Lab facilities. This decides "
    "which of your wallet balances is credited.",
    "Enter the Amount and a Remark, check the Recharge Summary and click Submit Recharge.",
    "The amount is credited to your IIC wallet automatically, usually within minutes, and you receive a "
    "confirmation email with the SRIC Ledger ID. To check at once, open Wallet > How to recharge on the "
    "Booking Portal and click Refresh.",
]
DIRECT_STEPS = [
    "Sign in to the Booking Portal and open Wallet, then click Recharge Wallet and select Direct Cash Deposit / "
    "Bank Transfer.",
    "Under Credit to, select the department, enter the Amount (minimum Rs. 100), read the undertaking that no "
    "project funds are available for this recharge and tick \"I agree to the above undertaking\".",
    "Click Send OTP, enter the OTP sent to your registered email and click Verify & Submit. Note the "
    "Transaction ID shown on screen (it is also emailed to you).",
    "Deposit the amount at the SRIC Bill Section, or complete the bank transfer, quoting this Transaction ID. "
    "The amount is credited to your wallet when the SRIC Bill Section approves the request.",
]


def notice_content(req: WalletRechargeRequest, *, recipient_name: str, reference: str, balance_after, credited_on, reversed_on) -> dict[str, str]:
    from iic_booking.communication.email_branding import format_email_datetime, format_inr, wrap_email_html

    links = _links()
    txn = req.transaction_number
    amount = format_inr(req.amount)
    dept = req.department.name if req.department_id else "—"
    subject = f"[{txn}] Wallet recharge of {amount} reversed — please recharge through the SRIC Portal (rnd.iitr.ac.in)"
    intro = [
        f"Your wallet recharge request {txn} for {amount} (Recharge via Project Grant), which was credited to your "
        f"{dept} wallet on {format_email_datetime(credited_on)}, has been reversed and the amount has been "
        "debited from your wallet.",
        "A Project Grant recharge is credited only after the SRIC Office approves it and the funds are confirmed "
        "in the SRIC cash-book. This request was approved from the requester's own Booking Portal account; it "
        "was not approved by the SRIC Office, and no matching SRIC cash-book receipt has been recorded. As SRIC "
        "has not confirmed it, no transfer from your project has been recorded against this request.",
        "Project Grant requests raised on the Booking Portal have been discontinued. Please recharge your "
        "wallet afresh using one of the options below.",
    ]
    rows = [
        ("Transaction ID", txn),
        ("Amount reversed", amount),
        ("Department sub-wallet", dept),
        ("Originally credited on", format_email_datetime(credited_on)),
        ("Reversed on", format_email_datetime(reversed_on)),
        ("Ledger reference", reference),
        ("Wallet balance after reversal", format_inr(balance_after)),
        ("Reason", REVERSAL_REASON),
    ]
    sric_steps = [s.format(sric=links["sric"]) for s in SRIC_STEPS]
    contact = [
        f"Support Ticket: sign in and open Support Tickets ({links['tickets']}), quoting {txn}.",
        f"Email: {links['support']}",
        "SRIC Portal access or project ledger: please contact the SRIC Office.",
    ]

    text = "\n".join(
        [f"Dear {recipient_name},", "", *[p + "\n" for p in intro]]
        + [f"{k}: {v}" for k, v in rows]
        + ["", "WHAT TO DO NOW", "", "Option A — Recharge from project funds through the SRIC Portal (faculty / Principal Investigator):"]
        + [f"{i}. {s}" for i, s in enumerate(sric_steps, 1)]
        + [f"Step-by-step guide: {links['guide']}", "", "Option B — No project funds available: Direct Cash Deposit / Bank Transfer:"]
        + [f"{i}. {s}" for i, s in enumerate(DIRECT_STEPS, 1)]
        + ["", "HELP AND CONTACT", *[f"- {c}" for c in contact], "", f"Your wallet: {links['wallet']}", "",
           "With regards,", "Head, Institute Instrumentation Centre", "Indian Institute of Technology Roorkee"]
    )

    p = "margin:0 0 12px 0;"
    table = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#475569;vertical-align:top;'><strong>{escape(k)}</strong></td>"
        f"<td style='padding:6px 0;color:#0f172a;'>{escape(str(v))}</td></tr>"
        for k, v in rows
    )

    def ol(items):
        return "<ol style='margin:0 0 12px 18px;padding:0;'>" + "".join(
            f"<li style='margin:0 0 6px 0;'>{escape(i)}</li>" for i in items
        ) + "</ol>"

    sric_html = ol(sric_steps).replace(
        escape(links["sric"]), f"<a href='{escape(links['sric'])}' style='color:#1d72af;font-weight:700;'>rnd.iitr.ac.in</a>", 1
    )
    body = (
        f"<p style='{p}'>Dear {escape(recipient_name)},</p>"
        + "".join(f"<p style='{p}'>{escape(t)}</p>" for t in intro)
        + f"<table cellpadding='0' cellspacing='0' style='font-family:Arial,Helvetica,sans-serif;font-size:14px;margin:0 0 16px 0;'>{table}</table>"
        + "<h3 style='margin:18px 0 8px 0;font-size:16px;color:#11294a;'>Option A — Recharge from project funds through the SRIC Portal</h3>"
        + sric_html
        + f"<p style='{p}'><a href='{escape(links['guide'])}' style='color:#1d72af;'>Step-by-step guide on the Booking Portal</a></p>"
        + "<h3 style='margin:18px 0 8px 0;font-size:16px;color:#11294a;'>Option B — No project funds available: Direct Cash Deposit / Bank Transfer</h3>"
        + ol(DIRECT_STEPS)
        + "<h3 style='margin:18px 0 8px 0;font-size:16px;color:#11294a;'>Help and contact</h3>"
        + "<ul style='margin:0 0 12px 18px;padding:0;'>" + "".join(f"<li style='margin:0 0 6px 0;'>{escape(c)}</li>" for c in contact) + "</ul>"
        + f"<p style='margin:18px 0 12px 0;'><a href='{escape(links['wallet'])}' style='color:#1d72af;'>View your wallet</a></p>"
        + f"<p style='{p}'>With regards,<br/>Head, Institute Instrumentation Centre<br/>Indian Institute of Technology Roorkee</p>"
    )
    html = wrap_email_html(
        title=f"Wallet recharge {txn} reversed",
        subtitle=f"{amount} · {reference}",
        preheader="Not confirmed by SRIC. Please recharge through the SRIC Portal (rnd.iitr.ac.in).",
        body_inner_html=body,
    )
    return {"subject": subject, "text": text, "html": html}


def send_notice(req: WalletRechargeRequest, *, actor: User | None = None) -> dict[str, Any]:
    """Email + in-portal notice to the wallet owner and requester, once per reversed request."""
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment
    from iic_booking.users.wallet_recharge_workflow import append_audit_log
    from iic_booking.users.display import get_user_display_name

    req = WalletRechargeRequest.objects.select_related("user", "wallet__user", "department").get(pk=req.pk)
    entry = req.audit_logs.filter(action=AUDIT_ACTION).order_by("-created_at").first()
    if entry is None:
        raise ReversalError(f"{req.transaction_number} has not been reversed; no notice sent.")
    if req.audit_logs.filter(action=NOTICE_ACTION).exists():
        return {"sent": 0, "already_sent": True}
    adj = WalletAdminAdjustment.objects.get(client_request_id=client_request_id(req))
    credited_on = entry.metadata.get("previous", {}).get("wallet_credited_at") or req.wallet_credited_at
    recipients = notice_recipients(req)
    sent = 0
    roles = []
    for user in recipients:
        content = notice_content(
            req,
            recipient_name=get_user_display_name(user) or "Sir / Madam",
            reference=adj.reference,
            balance_after=adj.balance_after,
            credited_on=credited_on,
            reversed_on=adj.created_at,
        )
        _send(user.email, content["subject"], content["text"], content["html"])
        sent += 1
        roles.append("owner" if user.pk == req.wallet.user_id else "requester")
    WalletAdminAdjustment.objects.filter(pk=adj.pk).update(email_sent_at=timezone.now())
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            recipients,
            title="Wallet recharge reversed",
            message=(
                f"{req.transaction_number} ({adj.amount:,.2f}) was reversed: not confirmed by SRIC. "
                "Please recharge through the SRIC Portal (rnd.iitr.ac.in > Ledger > New Wallet Recharge)."
            ),
            link="/wallet/recharge-from-project",
            notification_type="warning",
            event="wallet.recharge_reversed",
            extra={"wallet_recharge_request_id": req.pk, "wallet_admin_adjustment_id": adj.pk},
        )
        in_app = True
    except Exception:  # noqa: BLE001
        logger.exception("in-app reversal notice failed for recharge %s", req.pk)
        in_app = False
    append_audit_log(
        req,
        action=NOTICE_ACTION,
        from_status=req.status,
        to_status=req.status,
        actor=actor,
        message=f"Reversal notice emailed to {', '.join(roles)}.",
        metadata={"recipients": roles, "in_app": in_app, "adjustment_reference": adj.reference},
    )
    return {"sent": sent, "already_sent": False, "roles": roles, "in_app": in_app}
