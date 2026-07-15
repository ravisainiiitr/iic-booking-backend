"""
Wallet recharge operational helpers: SRIC email, admin/finance alerts, faculty pipeline matching hints.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone as django_timezone

from iic_booking.communication.models import CommunicationLog, CommunicationTemplate
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url

from .models.user_type import UserType
from .models.wallet import (
    WalletRechargeParseEntry,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from .models.wallet_sric_settings import WalletSricSettings

if TYPE_CHECKING:
    from django.http import HttpRequest

User = get_user_model()
logger = logging.getLogger(__name__)


def _parse_sric_recipient_emails(raw: str) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[\s,;]+", str(raw).strip())
    return [p.strip() for p in parts if p.strip() and "@" in p]


def _ordinal_day(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _format_sric_request_date_display(created_at) -> str:
    if not created_at:
        return ""
    dt = django_timezone.localtime(created_at)
    return f"{_ordinal_day(dt.day)} {dt.strftime('%B %Y at %H:%M:%S')}"


def sric_faculty_recharge_email_context(
    http_request: "HttpRequest",
    recharge_request: WalletRechargeRequest,
) -> dict[str, Any]:
    from django.urls import reverse

    user = recharge_request.user
    amount = recharge_request.amount
    department_name = recharge_request.department.name if recharge_request.department else "No Department"
    department_code = (
        recharge_request.department.code
        if recharge_request.department and recharge_request.department.code
        else ""
    )
    project_name = recharge_request.project.name if recharge_request.project else ""
    project_code = recharge_request.project.project_code if recharge_request.project else ""
    project_agency = recharge_request.project.agency if recharge_request.project else ""

    faculty_name = (user.name or user.email or "").strip()
    if faculty_name and not faculty_name.lower().startswith("prof"):
        faculty_display_name = f"Prof. {faculty_name}"
    else:
        faculty_display_name = faculty_name or "Faculty"
    emp_id = (getattr(user, "emp_id", None) or "").strip() or "N/A"

    approve_url = http_request.build_absolute_uri(
        reverse("users:approve-recharge-request", kwargs={"request_id": recharge_request.id})
    )
    reject_url = http_request.build_absolute_uri(
        reverse("users:reject-recharge-request", kwargs={"request_id": recharge_request.id})
    )

    sric_settings = WalletSricSettings.get_singleton()
    grant_code = resolve_department_grant_code(recharge_request.department)

    return {
        "faculty_name": faculty_name,
        "faculty_display_name": faculty_display_name,
        "grant_code_for_credit": grant_code,
        "emp_id": emp_id,
        "user_name": faculty_name,
        "user_email": user.email,
        "amount": f"{amount:.2f}",
        "request_id": str(recharge_request.id),
        "request_date": (
            recharge_request.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if recharge_request.created_at
            else ""
        ),
        "request_date_display": _format_sric_request_date_display(recharge_request.created_at),
        "project_name": project_name,
        "project_code": project_code,
        "project_agency": project_agency,
        "project_details": recharge_request.project_details or "",
        "department_name": department_name,
        "department_code": department_code,
        "approve_url": approve_url,
        "reject_url": reject_url,
    }


def resolve_department_grant_code(department) -> str:
    """Grant code for SRIC transfer: department internal_grant_code, else global SRIC settings."""
    if department:
        code = (getattr(department, "internal_grant_code", None) or "").strip()
        if code:
            return code
    sric_settings = WalletSricSettings.get_singleton()
    return (getattr(sric_settings, "grant_code_for_credit", None) or "").strip() or "IIC-000-002"


def create_sric_transfer_request(recharge_request: WalletRechargeRequest) -> Optional["SricTransferRequest"]:
    """Create API-visible SRIC transfer row (idempotent per recharge request)."""
    from .models.payment import SricTransferRequest, SricTransferRequestStatus

    if recharge_request.user.user_type != UserType.FACULTY:
        return None
    if not recharge_request.department_id:
        return None
    existing = getattr(recharge_request, "sric_transfer_request", None)
    if existing:
        return existing
    user = recharge_request.user
    dept = recharge_request.department
    grant = resolve_department_grant_code(dept)
    project = recharge_request.project
    return SricTransferRequest.objects.create(
        wallet_recharge_request=recharge_request,
        department=dept,
        grant_code=grant,
        amount=recharge_request.amount,
        faculty_emp_id=(getattr(user, "emp_id", None) or "").strip(),
        faculty_email=user.email,
        faculty_name=(user.name or "").strip(),
        project_code=(project.project_code if project else "") or "",
        project_name=(project.name if project else "") or "",
        status=SricTransferRequestStatus.PENDING,
    )


def send_sric_faculty_recharge_email(
    http_request: "HttpRequest",
    recharge_request: WalletRechargeRequest,
    *,
    recipients: list[str],
) -> tuple[bool, Optional[str]]:
    """
    Send SRIC Office email for a faculty wallet recharge request.
    Does not modify recharge_request. Caller sets sric_notification_sent after success.
    """
    if not recipients:
        return False, "No SRIC recipients"

    template_context = sric_faculty_recharge_email_context(http_request, recharge_request)
    amount = recharge_request.amount
    user = recharge_request.user
    faculty_email = (user.email or "").strip()
    faculty_display = template_context["faculty_display_name"]
    emp_id = template_context["emp_id"]

    template_obj = CommunicationService.get_template(
        template="wallet_recharge_sric_office_email",
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
    )

    if template_obj:
        rendered = CommunicationService.render_template(template_obj, context=template_context)
        subject = rendered.get(
            "subject",
            f"Urgent IIC Testing Grant ({template_context['grant_code_for_credit']}) credit request — "
            f"{faculty_display} [{emp_id}]",
        )
        message = rendered.get("message", "")
        html_message = rendered.get("html_message", "")
    else:
        ctx = template_context
        gc = ctx["grant_code_for_credit"]
        subject = (
            f"Urgent IIC Testing Grant ({gc}) credit request — {faculty_display} [{emp_id}]"
        )
        message = (
            f"Urgent IIC Testing Grant ({gc}) credit request\n\n"
            f"Faculty Name: {ctx['faculty_display_name']}\n"
            f"Employee number: {ctx['emp_id']}\n"
            f"Email: {ctx['user_email']}\n"
            f"Amount: ₹{ctx['amount']}\n"
            f"Department: {ctx['department_name']} ({ctx['department_code']})\n"
            f"Project Name: {ctx['project_name']} ({ctx['project_code']})\n"
            f"Agency: {ctx['project_agency']}\n"
            f"Requested at: {ctx['request_date_display']}\n\n"
            f"Grant code for Credit: {gc}\n\n"
            f"The accounts team notification may already have been sent separately. "
            f"This message is for SRIC Office awareness.\n\n"
            f"Thank you."
        )
        html_message = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head>"
            "<body style=\"font-family: Arial, sans-serif; line-height: 1.6; color: #333;\">"
            f"<p><strong>Urgent IIC Testing Grant ({gc}) credit request</strong></p>"
            "<table cellpadding=\"6\" style=\"border-collapse: collapse;\">"
            f"<tr><td><strong>Faculty Name:</strong></td><td>{ctx['faculty_display_name']}</td></tr>"
            f"<tr><td><strong>Employee number:</strong></td><td>{ctx['emp_id']}</td></tr>"
            f"<tr><td><strong>Email:</strong></td><td>{ctx['user_email']}</td></tr>"
            f"<tr><td><strong>Amount:</strong></td><td>₹{ctx['amount']}</td></tr>"
            f"<tr><td><strong>Department:</strong></td><td>{ctx['department_name']} ({ctx['department_code']})</td></tr>"
            f"<tr><td><strong>Project Name:</strong></td><td>{ctx['project_name']} ({ctx['project_code']})</td></tr>"
            f"<tr><td><strong>Agency:</strong></td><td>{ctx['project_agency']}</td></tr>"
            f"<tr><td><strong>Requested at:</strong></td><td>{ctx['request_date_display']}</td></tr>"
            f"<tr><td><strong>Grant code for Credit:</strong></td><td>{gc}</td></tr>"
            "</table></body></html>"
        )

    to_list = list(recipients)
    cc_list: list[str] = []
    if faculty_email and faculty_email.lower() not in {e.lower() for e in to_list}:
        from iic_booking.users.test_accounts import redirect_email_for_user

        cc_addr, _ = redirect_email_for_user(user, original_email=faculty_email, subject=None)
        if cc_addr and cc_addr.lower() not in {e.lower() for e in to_list}:
            cc_list.append(cc_addr)

    try:
        email_msg = EmailMultiAlternatives(
            subject=subject[:250],
            body=message or subject,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to_list,
            cc=cc_list or None,
        )
        if html_message:
            email_msg.attach_alternative(html_message, "text/html")
        email_msg.send(fail_silently=False)
    except Exception as e:
        return False, str(e)

    try:
        if template_obj:
            CommunicationLog.objects.create(
                communication_type=CommunicationLog.CommunicationType.EMAIL,
                recipient=user,
                recipient_email=(
                    f"To: {', '.join(recipients)}"
                    + (f" | CC: {', '.join(cc_list)}" if cc_list else "")
                )[:255],
                template=template_obj,
                subject=subject[:255],
                message=message,
                status=CommunicationLog.CommunicationStatus.SENT,
                sent_at=django_timezone.now(),
                metadata={
                    "wallet_recharge_request_id": recharge_request.id,
                    "sric_recipients": recipients,
                    "cc_faculty": cc_list,
                },
                created_by=user,
            )
    except Exception:
        pass

    return True, None


def notify_admin_finance_new_wallet_recharge_request(
    http_request: "HttpRequest",
    recharge_request: WalletRechargeRequest,
) -> None:
    """Email all active Admin and Accounts-in-Charge (finance) app users about a new OTP-verified request."""
    staff_emails = set(
        User.objects.filter(
            user_type__in=[UserType.ADMIN, UserType.FINANCE],
            is_active=True,
            is_test_account=False,
        )
        .exclude(email__isnull=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not staff_emails:
        return

    u = recharge_request.user
    emp = (getattr(u, "emp_id", None) or "").strip() or "—"
    dept = recharge_request.department.name if recharge_request.department else "—"
    proj = ""
    if recharge_request.project:
        proj = f"{recharge_request.project.name} ({recharge_request.project.project_code})"
    elif recharge_request.project_details:
        proj = recharge_request.project_details[:200]

    parse_link = get_frontend_absolute_url("/admin-settings/wallet-recharge-parse")
    subject = f"[IIC] Wallet recharge request #{recharge_request.id} — ₹{recharge_request.amount} — Emp {emp}"
    body_lines = [
        "A wallet recharge request was submitted (user OTP verified).",
        "",
        f"Request ID: {recharge_request.id}",
        f"Requester: {u.name or u.email} <{u.email}>",
        f"Employee No.: {emp}",
        f"Amount: ₹{recharge_request.amount}",
        f"Department (sub-wallet): {dept}",
        f"Project: {proj or '—'}",
        f"SRIC notification sent: {'Yes' if recharge_request.sric_notification_sent else 'No'}",
        "",
        f"Wallet recharge parse & history: {parse_link}",
        "",
        "After SRIC sends the accounts file, parsed rows on that page can auto-match this request by amount + Emp No.",
    ]
    body = "\n".join(body_lines)

    try:
        msg = EmailMultiAlternatives(
            subject=subject[:250],
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[sorted(staff_emails)[0]],
            bcc=sorted(staff_emails)[1:] if len(staff_emails) > 1 else None,
        )
        msg.send(fail_silently=False)
    except Exception as e:
        logger.warning("notify_admin_finance_new_wallet_recharge_request failed: %s", e)


def try_auto_sric_and_staff_alerts_after_recharge_verified(
    http_request: "HttpRequest",
    recharge_request: WalletRechargeRequest,
) -> None:
    """
    After faculty OTP verification on a recharge request:
    - Create SRIC transfer API record (preferred when SRIC_API_KEY is configured).
    - Optionally send SRIC email if recipients configured and email fallback enabled.
    - Email Admin + Finance users with a link to the parse workspace.
    """
    from django.conf import settings as django_settings

    if recharge_request.user.user_type == UserType.FACULTY:
        create_sric_transfer_request(recharge_request)
        use_api = bool((getattr(django_settings, "SRIC_API_KEY", "") or "").strip())
        email_fallback = getattr(django_settings, "SRIC_EMAIL_FALLBACK", False)
        sric_settings = WalletSricSettings.get_singleton()
        recipients = _parse_sric_recipient_emails(sric_settings.recipient_emails)
        if recipients and not recharge_request.sric_notification_sent and (email_fallback or not use_api):
            ok, _err = send_sric_faculty_recharge_email(
                http_request, recharge_request, recipients=recipients
            )
            if ok:
                WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(
                    sric_notification_sent=True,
                    updated_at=django_timezone.now(),
                )
                recharge_request.sric_notification_sent = True
        elif use_api and not recharge_request.sric_notification_sent:
            WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(
                sric_notification_sent=True,
                updated_at=django_timezone.now(),
            )
            recharge_request.sric_notification_sent = True

    notify_admin_finance_new_wallet_recharge_request(http_request, recharge_request)


def parse_entry_amount_decimal(entry: WalletRechargeParseEntry) -> Optional[Decimal]:
    try:
        return Decimal(str((entry.amount or "").replace(",", "").strip()))
    except Exception:
        return None


def wallet_recharge_parse_match_index(emp_nos: set[str]) -> dict[str, set[Decimal]]:
    """One query: map normalized emp_no -> parse-entry amounts (for pipeline UI, avoids N+1)."""
    if not emp_nos:
        return {}
    out: dict[str, set[Decimal]] = defaultdict(set)
    qs = WalletRechargeParseEntry.objects.filter(emp_no__in=emp_nos).only("emp_no", "amount")
    for e in qs:
        emp = (e.emp_no or "").strip()
        if not emp:
            continue
        d = parse_entry_amount_decimal(e)
        if d is not None:
            out[emp].add(d)
    return dict(out)


def recharge_request_matches_parse_index(
    req: WalletRechargeRequest, index: dict[str, set[Decimal]]
) -> bool:
    """True if index (from wallet_recharge_parse_match_index) contains req amount for req user's emp."""
    emp = (req.user.emp_id or "").strip()
    if not emp:
        return False
    return req.amount in index.get(emp, ())


def recharge_request_has_matching_parse_row(req: WalletRechargeRequest) -> bool:
    """True if a stored parse entry exists with same Emp No. and amount as the request (awaiting or extra SRIC row)."""
    emp = (req.user.emp_id or "").strip()
    if not emp:
        return False
    index = wallet_recharge_parse_match_index({emp})
    return recharge_request_matches_parse_index(req, index)
