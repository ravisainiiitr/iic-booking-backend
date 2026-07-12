"""Wallet event notification service."""

import logging
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.utils import timezone

from iic_booking.users.models import WalletRechargeRequest
from .service import CommunicationService
from .utils import get_frontend_absolute_url, booking_display_id_for_email

logger = logging.getLogger(__name__)


def send_sub_wallet_transaction_notifications(
    transaction,
    booking=None,
    booking_user=None,
    cc_emails: Optional[list] = None,
) -> None:
    """
    Send email notifications for a sub-wallet transaction to both booking user and Supervisor.
    
    Args:
        transaction: SubWalletTransaction instance
        booking: Optional Booking instance (if transaction is related to a booking)
        booking_user: Optional User instance (if transaction is related to a booking but booking object not available)
    """
    if not transaction or not getattr(transaction, "sub_wallet", None):
        return
    
    sub_wallet = transaction.sub_wallet
    if not sub_wallet or not sub_wallet.wallet or not sub_wallet.wallet.user:
        return
    
    wallet_owner = sub_wallet.wallet.user
    transaction_type = getattr(transaction, "transaction_type", "")
    amount = transaction.amount
    description = getattr(transaction, "description", "")
    
    # Determine if this is a booking-related transaction
    is_booking_related = booking is not None or booking_user is not None
    booking_display_ref = ""
    if is_booking_related:
        if booking:
            booking_user = booking.user
            booking_id = booking.booking_id
            booking_display_ref = booking_display_id_for_email(booking)
            equipment_name = booking.equipment.name
            equipment_code = booking.equipment.code
        else:
            booking_id = None
            equipment_name = None
            equipment_code = None
            # Try to extract booking info from description
            if "Booking #" in description:
                try:
                    booking_id = int(description.split("Booking #")[1].split()[0])
                except (ValueError, IndexError):
                    pass
            if booking_id is not None:
                try:
                    from iic_booking.equipment.models import Booking as BookingModel

                    b = BookingModel.objects.select_related("equipment").filter(pk=booking_id).first()
                    if b:
                        booking_display_ref = booking_display_id_for_email(b)
                except Exception:
                    pass
            if not booking_display_ref and booking_id is not None:
                booking_display_ref = str(booking_id)
    else:
        booking_user = None
        booking_id = None
        equipment_name = None
        equipment_code = None
    
    # Absolute link for emails and in-app (relative links do not work in email clients)
    link = get_frontend_absolute_url("/wallet")
    if is_booking_related and booking_id:
        link = get_frontend_absolute_url(f"/my-bookings?booking={booking_id}")
    
    # Prepare context for Supervisor
    wallet_context = {
        "user_name": wallet_owner.name or wallet_owner.email,
        "user_email": wallet_owner.email,
        "transaction_type": transaction_type,
        "transaction_type_display": "Credit" if transaction_type == "credit" else "Debit",
        "amount": f"{amount:.2f}",
        "description": description,
        "balance": f"{sub_wallet.balance:.2f}",
        "department_name": sub_wallet.department.name if sub_wallet.department else "No Department",
        "department_code": sub_wallet.department.code if sub_wallet.department and sub_wallet.department.code else "",
        "transaction_date": transaction.created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(transaction, "created_at") and transaction.created_at else "",
        "link": link,
    }
    
    # Add booking info if available (booking_id in templates = public display id, not DB pk)
    if is_booking_related:
        wallet_context.update({
            "booking_id": booking_display_ref or (str(booking_id) if booking_id else ""),
            "equipment_name": equipment_name or "",
            "equipment_code": equipment_code or "",
            "is_booking_related": True,
        })
    else:
        wallet_context["is_booking_related"] = False
    
    # Metadata for communication log
    wallet_metadata = {
        "transaction_id": transaction.id if hasattr(transaction, "id") else None,
        "sub_wallet_id": sub_wallet.id,
        "transaction_type": transaction_type,
        "amount": str(amount),
        "balance": str(sub_wallet.balance),
        "notification_type": "info",
        "link": link,
    }
    
    if booking_id:
        wallet_metadata["booking_id"] = booking_id
        if booking_display_ref:
            wallet_metadata["booking_display_id"] = booking_display_ref
    
    # Determine template codes
    if transaction_type == "credit":
        email_template_code = "wallet_credit_email"
        push_template_code = "wallet_credit_push"
        title = f"Wallet Credited - ₹{amount:.2f}"
        message = f"Your wallet has been credited with ₹{amount:.2f}"
    else:  # debit
        email_template_code = "wallet_debit_email"
        push_template_code = "wallet_debit_push"
        title = f"Wallet Debited - ₹{amount:.2f}"
        message = f"₹{amount:.2f} has been debited from your wallet"
    
    if is_booking_related:
        ref = booking_display_ref or (str(booking_id) if booking_id else "")
        message += f" for Booking #{ref}" if ref else " for booking"
    
    # Send email notification to Supervisor
    try:
        CommunicationService.send_email(
            recipient=wallet_owner,
            template=email_template_code,
            template_context=wallet_context,
            metadata=wallet_metadata,
            cc_emails=cc_emails,
        )
        logger.info(f"Wallet transaction email notification sent to Supervisor {wallet_owner.email}")
    except Exception as e:
        logger.error(
            f"Failed to send wallet transaction email to Supervisor {wallet_owner.email}: {str(e)}",
            exc_info=True,
        )
    
    # Send email notification to booking user (if different from Supervisor)
    if is_booking_related and booking_user and booking_user != wallet_owner:
        booking_context = wallet_context.copy()
        booking_context.update({
            "user_name": booking_user.name or booking_user.email,
            "user_email": booking_user.email,
        })
        
        booking_metadata = wallet_metadata.copy()
        booking_metadata["notification_type"] = "info"
        
        try:
            CommunicationService.send_email(
                recipient=booking_user,
                template=email_template_code,
                template_context=booking_context,
                metadata=booking_metadata,
            )
            logger.info(f"Wallet transaction email notification sent to booking user {booking_user.email}")
        except Exception as e:
            logger.error(
                f"Failed to send wallet transaction email to booking user {booking_user.email}: {str(e)}",
                exc_info=True,
            )


def send_wallet_low_balance_notification(
    user,
    balance: Decimal,
    threshold: Optional[Decimal] = None,
) -> None:
    """
    Send notification when wallet balance is low.
    
    Args:
        user: User instance
        balance: Current wallet balance
        threshold: Optional threshold amount (defaults to settings value)
    """
    if threshold is None:
        threshold = getattr(settings, "WALLET_LOW_BALANCE_THRESHOLD", Decimal("100.00"))
    
    if balance >= threshold:
        # Balance is not low enough
        return
    
    link = get_frontend_absolute_url("/wallet")
    
    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "balance": f"{balance:.2f}",
        "threshold": f"{threshold:.2f}",
        "link": link,
    }
    
    metadata = {
        "balance": str(balance),
        "threshold": str(threshold),
        "notification_type": "warning",
        "link": link,
    }
    
    # Send email notification
    try:
        CommunicationService.send_email(
            recipient=user,
            template="wallet_low_balance_email",
            template_context=context,
            metadata=metadata,
        )
        logger.info(f"Wallet low balance email notification sent to {user.email}")
    except Exception as e:
        logger.error(
            f"Failed to send wallet low balance email to {user.email}: {str(e)}",
            exc_info=True,
        )
    
    # Send push notification
    try:
        # Prepare title and message as fallback if template is not found
        title = "Low Wallet Balance"
        message = f"Your wallet balance ({balance:.2f}) is below the threshold ({threshold:.2f})"
        
        CommunicationService.send_push_notification(
            recipient=user,
            template="wallet_low_balance_push",
            template_context=context,
            metadata=metadata,
            title=title,  # Fallback title
            message=message,  # Fallback message
        )
        logger.info(f"Wallet low balance push notification sent to {user.email}")
    except Exception as e:
        logger.error(
            f"Failed to send wallet low balance push to {user.email}: {str(e)}",
            exc_info=True,
        )


def send_wallet_recharge_request_notifications(
    recharge_request: WalletRechargeRequest,
    status: str,
) -> None:
    """
    Send email and push notifications for wallet recharge request status changes.
    
    Args:
        recharge_request: WalletRechargeRequest instance
        status: Status change (APPROVED, REJECTED, etc.)
    """
    if not recharge_request or not recharge_request.user:
        logger.warning("Invalid recharge request or user for notification")
        return
    
    user = recharge_request.user
    amount = recharge_request.amount
    balance = recharge_request.wallet.total_balance
    
    # Prepare template context
    department_name = recharge_request.department.name if recharge_request.department else "No Department"
    department_code = recharge_request.department.code if recharge_request.department and recharge_request.department.code else ""
    
    # Absolute link for emails and in-app
    link = get_frontend_absolute_url(f"/wallet/recharge-requests/{recharge_request.id}")
    
    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "amount": f"{amount:.2f}",
        "balance": f"{balance:.2f}",
        "request_id": str(recharge_request.id),
        "request_date": recharge_request.created_at.strftime("%Y-%m-%d %H:%M:%S") if recharge_request.created_at else "",
        "project_details": recharge_request.project_details or "",
        "status": status,
        "response_message": recharge_request.response_message or "",
        "approved_by_email": recharge_request.approved_by_email or "",
        "department_name": department_name,
        "department_code": department_code,
        "link": link,
    }
    
    # Metadata for communication log
    metadata = {
        "wallet_recharge_request_id": recharge_request.id,
        "status": status,
        "amount": str(amount),
        "balance": str(balance),
        "notification_type": "info" if status == "APPROVED" else "warning" if status == "REJECTED" else "info",
        "link": link,
    }
    
    # Determine template codes based on status
    if status == "APPROVED":
        email_template_code = "wallet_recharge_approved_email"
        push_template_code = "wallet_recharge_approved_push"
        title = "Wallet Recharge Approved"
        message = f"Your wallet recharge request of ₹{amount:.2f} has been approved. New balance: ₹{balance:.2f}"
    elif status == "REJECTED":
        email_template_code = "wallet_recharge_rejected_email"
        push_template_code = "wallet_recharge_rejected_push"
        title = "Wallet Recharge Rejected"
        message = f"Your wallet recharge request of ₹{amount:.2f} has been rejected."
    else:
        # For PENDING or other statuses
        email_template_code = "wallet_recharge_pending_email"
        push_template_code = "wallet_recharge_pending_push"
        title = "Wallet Recharge Request"
        message = f"Your wallet recharge request of ₹{amount:.2f} is pending approval."
    
    # Send email notification
    try:
        CommunicationService.send_email(
            recipient=user,
            template=email_template_code,
            template_context=context,
            metadata=metadata,
        )
        logger.info(f"Wallet recharge {status} email notification sent to {user.email}")
    except ValueError as e:
        # Template not found - use fallback email
        logger.warning(f"Template '{email_template_code}' not found, using fallback email for {user.email}")
        try:
            from django.core.mail import send_mail
            from django.conf import settings
            
            # Create fallback email content
            fallback_subject = title
            department_info = ""
            if context.get('department_name') and context['department_name'] != "No Department":
                dept_code = f" ({context['department_code']})" if context.get('department_code') else ""
                department_info = f"- Department: {context['department_name']}{dept_code}\n"
            
            fallback_message = f"""
Hello {context['user_name']},

{message}

Request Details:
- Amount: ₹{context['amount']}
{department_info}- Request ID: {context['request_id']}
- Request Date: {context['request_date']}
{f"- Project Details: {context['project_details']}" if context.get('project_details') else ""}
{f"- Response: {context['response_message']}" if context.get('response_message') else ""}
{f"- Approved By: {context['approved_by_email']}" if context.get('approved_by_email') else ""}

Thank you for using IIC Booking!
            """.strip()
            
            send_mail(
                subject=fallback_subject,
                message=fallback_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            logger.info(f"Wallet recharge {status} fallback email sent to {user.email}")
        except Exception as fallback_error:
            logger.error(
                f"Failed to send wallet recharge {status} fallback email to {user.email}: {str(fallback_error)}",
                exc_info=True,
            )
    except Exception as e:
        logger.error(
            f"Failed to send wallet recharge {status} email to {user.email}: {str(e)}",
            exc_info=True,
        )
    
    # Send push notification
    try:
        CommunicationService.send_push_notification(
            recipient=user,
            template=push_template_code,
            template_context=context,
            metadata=metadata,
            title=title,  # Fallback title
            message=message,  # Fallback message
        )
        logger.info(f"Wallet recharge {status} push notification sent to {user.email}")
    except ValueError as e:
        # Template not found - push notifications already have fallback title/message
        logger.warning(f"Template '{push_template_code}' not found, push notification skipped for {user.email}: {str(e)}")
    except Exception as e:
        logger.error(
            f"Failed to send wallet recharge {status} push to {user.email}: {str(e)}",
            exc_info=True,
        )


def send_wallet_credit_facility_activated_user_email(recharge_request: WalletRechargeRequest) -> None:
    """
    Faculty accepted the temporary credit line; facility just became ACTIVE after OTP verify.
    Emails the wallet user only (no SRIC office, no CC) with copy aligned to recharge request emails.
    """
    req = (
        WalletRechargeRequest.objects.select_related("user", "department", "project", "wallet")
        .filter(pk=recharge_request.pk)
        .first()
    )
    if not req or not req.user or not getattr(req.user, "email", None):
        logger.warning("Credit facility email skipped: missing request or user email (pk=%s)", getattr(recharge_request, "pk", None))
        return

    user = req.user
    amount = req.amount
    department_name = req.department.name if req.department else "No Department"
    dept_code = (req.department.code or "").strip() if req.department else ""
    department_code_suffix = f" ({dept_code})" if dept_code else ""

    project_lines_plain = ""
    project_lines_html = ""
    if req.project_id and req.project:
        p = req.project
        pn = (p.name or "").strip()
        pc = (p.project_code or "").strip()
        pa = (p.agency or "").strip()
        if pn or pc:
            line = f"- Project: {pn}" + (f" ({pc})" if pc else "")
            project_lines_plain += line + "\n"
        if pa:
            project_lines_plain += f"- Agency: {pa}\n"
        if pn or pc:
            project_lines_html += '<div class="detail-row"><span class="label">Project:</span> '
            project_lines_html += pn + (f" ({pc})" if pc else "")
            project_lines_html += "</div>"
        if pa:
            project_lines_html += f'<div class="detail-row"><span class="label">Agency:</span> {pa}</div>'
    elif (req.project_details or "").strip():
        pd = (req.project_details or "").strip()[:800]
        project_lines_plain = f"- Details: {pd}\n"
        project_lines_html = f'<div class="detail-row"><span class="label">Details:</span> {pd}</div>'

    link = get_frontend_absolute_url(f"/wallet/recharge-requests/{req.id}")
    ends_at = req.credit_window_ends_at
    if ends_at:
        credit_window_end_display = timezone.localtime(ends_at).strftime("%d %B %Y, %H:%M %Z")
    else:
        credit_window_end_display = ""

    limit = req.credit_limit_amount or Decimal("0")
    credit_limit_amount = f"{limit.quantize(Decimal('0.01')):.2f}"

    from iic_booking.users.wallet_credit_facility import get_credit_settings

    credit_window_days = int(get_credit_settings().credit_window_days)

    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "amount": f"{amount:.2f}",
        "request_id": str(req.id),
        "request_date": req.created_at.strftime("%Y-%m-%d %H:%M:%S") if req.created_at else "",
        "department_name": department_name,
        "department_code_suffix": department_code_suffix,
        "project_lines_plain": project_lines_plain.rstrip(),
        "project_lines_html": project_lines_html,
        "credit_limit_amount": credit_limit_amount,
        "credit_window_end_display": credit_window_end_display,
        "credit_window_days": str(credit_window_days),
        "link": link,
    }
    metadata = {
        "wallet_recharge_request_id": req.id,
        "notification_type": "info",
        "wallet_credit_facility": "activated",
        "link": link,
    }
    template_code = "wallet_recharge_credit_facility_activated_email"

    try:
        CommunicationService.send_email(
            recipient=user,
            template=template_code,
            template_context=context,
            metadata=metadata,
        )
        logger.info("Credit facility activation email sent to %s (request %s)", user.email, req.id)
    except ValueError:
        logger.warning("Template %s missing; using fallback email for %s", template_code, user.email)
        try:
            from django.core.mail import send_mail

            subj = f"Wallet recharge — temporary credit facility active (Request #{req.id})"
            body = f"""Hello {context['user_name']},

You accepted the temporary credit facility for your pending wallet recharge. It is now active. This email is for your records only (SRIC office is not copied).

Recharge request
- Request ID: {context['request_id']}
- Amount: ₹{context['amount']}
- Department: {department_name}{department_code_suffix}
- Request date: {context['request_date']}
{project_lines_plain.rstrip()}

Credit facility
- Overdraft limit: ₹{credit_limit_amount} until {credit_window_end_display} ({credit_window_days} day window).

View request: {link}

Thank you for using IIC Booking System.
"""
            send_mail(
                subject=subj,
                message=body.strip(),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.error("Fallback credit facility email failed for %s: %s", user.email, e, exc_info=True)
    except Exception as e:
        logger.error("Credit facility activation email failed for %s: %s", user.email, e, exc_info=True)

