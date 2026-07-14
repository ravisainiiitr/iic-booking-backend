"""Booking event history and notification utilities."""

import html
import re
import logging
import threading
from typing import Optional, Dict, Any
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model

from .models import Booking, BookingEvent, BookingEventType, BookingStatus
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url, booking_display_id_for_email

logger = logging.getLogger(__name__)
User = get_user_model()

# Booking confirmation instruction appended to confirmation emails.
BOOKING_CONFIRMATION_INSTRUCTIONS = (
    "Sample Submission and Collection time is from 10:00 to 10:30 in morning and "
    "5:00 to 5:30 in the evening.\n"
    "No need to visit for the results as they will be available on you booking details "
    "Section when ready. You will be intimated via email regarding the same."
)

# Shown only when Equipment.sample_preparation_by_user is True and the recipient is an internal user.
USER_SAMPLE_PREPARATION_NOTICE_PLAIN = (
    "We would like to inform all internal users that, wherever feasible, sample preparation should be "
    "carried out by the student/researcher themselves, under the guidance and assistance of the "
    "laboratory operators. This helps in ensuring proper handling, safety, and optimal utilization of "
    "the equipment.\n\n"
    "Kindly note the following:\n\n"
    "- Users are encouraged to prepare their samples themselves, with support from laboratory operators "
    "wherever possible.\n"
    "- Laboratory operators will provide necessary guidance and supervision during the preparation process.\n"
    "- Improperly prepared samples may lead to inaccurate results or may not be accepted for analysis.\n"
    "- Users are requested to be present in the laboratory/office at 10:00 AM on the day of their booking.\n"
    "- Please plan your visit accordingly to allow sufficient time for sample preparation before your "
    "scheduled slot.\n"
    "- This approach is followed to help ensure timely delivery of analysis results and to minimize delays "
    "for other users.\n\n"
    "Your cooperation in adhering to these guidelines will help maintain efficiency and ensure "
    "high-quality results for all users.\n\n"
    "For any clarification, please feel free to contact the facility staff."
)

USER_SAMPLE_PREPARATION_NOTICE_HTML = (
    '<div style="margin-top:16px;padding:14px 16px;background:#f9f9f9;border-left:4px solid #4CAF50;">'
    "<p style=\"margin:0 0 12px 0;\">We would like to inform all internal users that, wherever feasible, "
    "sample preparation should be carried out by the student/researcher themselves, under the guidance "
    "and assistance of the laboratory operators. This helps in ensuring proper handling, safety, and optimal "
    "utilization of the equipment.</p>"
    "<p style=\"margin:0 0 8px 0;\"><strong>Kindly note the following:</strong></p>"
    "<ul style=\"margin:8px 0 16px 0;padding-left:20px;\">"
    "<li>Users are encouraged to prepare their samples themselves, with support from laboratory operators "
    "wherever possible.</li>"
    "<li>Laboratory operators will provide necessary guidance and supervision during the preparation process."
    "</li>"
    "<li>Improperly prepared samples may lead to inaccurate results or may not be accepted for analysis.</li>"
    "<li>Users are requested to be present in the laboratory/office at 10:00 AM on the day of their booking."
    "</li>"
    "<li>Please plan your visit accordingly to allow sufficient time for sample preparation before your "
    "scheduled slot.</li>"
    "<li>This approach is followed to help ensure timely delivery of analysis results and to minimize delays "
    "for other users.</li>"
    "</ul>"
    "<p style=\"margin:0 0 12px 0;\">Your cooperation in adhering to these guidelines will help maintain "
    "efficiency and ensure high-quality results for all users.</p>"
    "<p style=\"margin:0;\">For any clarification, please feel free to contact the facility staff.</p>"
    "</div>"
)


def _append_confirmation_instructions_to_context(context: dict, *, also_append_to_comment: bool = True) -> dict:
    """
    Add booking confirmation instructions to the template context.
    We keep it in a dedicated variable and also append to `comment` so existing templates
    show it even if they don't render the new variable.
    """
    if not isinstance(context, dict):
        return context
    context.setdefault("booking_confirmation_instructions", BOOKING_CONFIRMATION_INSTRUCTIONS)
    if also_append_to_comment:
        base = (str(context.get("comment", "") or "")).strip()
        if BOOKING_CONFIRMATION_INSTRUCTIONS not in base:
            context["comment"] = (base + ("\n\n" if base else "") + BOOKING_CONFIRMATION_INSTRUCTIONS).strip()
    return context


def apply_user_sample_preparation_notice_to_context(
    context: dict,
    user,
    equipment,
    *,
    also_append_to_comment: bool = False,
) -> dict:
    """
    When equipment.sample_preparation_by_user is set and the user is internal (student / individual
    student / faculty), set user_sample_preparation_notice (plain + HTML) and optionally append plain
    text to comment. External and other user types get empty strings.
    """
    if not isinstance(context, dict):
        return context
    from iic_booking.users.models.user_type import UserType

    context.setdefault("user_sample_preparation_notice", "")
    context.setdefault("user_sample_preparation_notice_html", "")

    ut = getattr(user, "user_type", None) or ""
    if not getattr(equipment, "sample_preparation_by_user", False):
        return context
    if UserType.is_external_user(ut) or not UserType.is_internal_user(ut):
        return context

    context["user_sample_preparation_notice"] = USER_SAMPLE_PREPARATION_NOTICE_PLAIN
    context["user_sample_preparation_notice_html"] = USER_SAMPLE_PREPARATION_NOTICE_HTML
    if also_append_to_comment:
        marker = "We would like to inform all internal users that"
        base = (str(context.get("comment", "") or "")).strip()
        if marker not in base:
            context["comment"] = (base + ("\n\n" if base else "") + USER_SAMPLE_PREPARATION_NOTICE_PLAIN).strip()
    return context


def apply_equipment_booking_email_extra_to_context(
    context: dict,
    equipment,
    *,
    also_append_to_comment: bool = False,
) -> dict:
    """
    Append equipment-specific plain text for booking/reminder emails.
    Sets equipment_booking_email_extra (plain) and equipment_booking_email_extra_html (escaped, for HTML
    bodies with white-space: pre-wrap). Optionally appends plain text to comment for templates that use it.
    """
    if not isinstance(context, dict):
        return context
    raw = (getattr(equipment, "booking_email_extra_text", None) or "").strip()
    context["equipment_booking_email_extra"] = raw
    context["equipment_booking_email_extra_html"] = html.escape(raw) if raw else ""
    if also_append_to_comment and raw:
        base = (str(context.get("comment", "") or "")).strip()
        context["comment"] = (base + ("\n\n" if base else "") + raw).strip()
    return context


def _linkify_plain_text_for_html(text: str) -> str:
    """Escape text and turn http(s) URLs into anchor tags for HTML email bodies."""
    if not text:
        return ""
    escaped = html.escape(text)
    url_pattern = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+")

    def _repl(match: re.Match) -> str:
        url = match.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'

    return url_pattern.sub(_repl, escaped).replace("\n", "<br>\n")


def apply_equipment_completion_email_extra_to_context(
    context: dict,
    equipment,
    *,
    also_append_to_comment: bool = False,
) -> dict:
    """Append equipment-specific completion email footer (plain + linkified HTML)."""
    if not isinstance(context, dict):
        return context
    raw = (getattr(equipment, "completion_email_extra_text", None) or "").strip()
    context["equipment_completion_email_extra"] = raw
    context["equipment_completion_email_extra_html"] = _linkify_plain_text_for_html(raw) if raw else ""
    if also_append_to_comment and raw:
        base = (str(context.get("comment", "") or "")).strip()
        context["comment"] = (base + ("\n\n" if base else "") + raw).strip()
    return context


def append_completion_email_extra_plaintext(message: str, equipment) -> str:
    raw = (getattr(equipment, "completion_email_extra_text", None) or "").strip()
    if not raw:
        return message or ""
    base = (message or "").rstrip()
    return f"{base}\n\n{raw}" if base else raw


def append_completion_email_extra_html(html_message: str, equipment) -> str:
    raw = (getattr(equipment, "completion_email_extra_text", None) or "").strip()
    if not raw:
        return html_message
    block = (
        "<hr style='margin:16px 0;border:none;border-top:1px solid #ddd;'/>"
        f"<div>{_linkify_plain_text_for_html(raw)}</div>"
    )
    if html_message:
        return html_message + block
    return f"<html><body>{block}</body></html>"


def create_booking_event(
    booking: Booking,
    event_type: str,
    created_by: Optional[User] = None,
    comment: Optional[str] = None,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    send_notification: bool = True,
    system_actor: bool = False,
) -> BookingEvent:
    """
    Create a booking event and optionally send notifications.
    
    Args:
        booking: Booking instance
        event_type: Type of event (from BookingEventType)
        created_by: User who created the event (defaults to booking user)
        comment: Optional comment/description
        previous_status: Previous booking status (if status changed)
        new_status: New booking status (if status changed)
        metadata: Additional metadata dictionary
        send_notification: Whether to send email and push notifications
        system_actor: If True and created_by is None, store null created_by (automated/system event).
        
    Returns:
        BookingEvent instance
    """
    if created_by is None and not system_actor:
        created_by = booking.user
    
    # Create the event
    event = BookingEvent.objects.create(
        booking=booking,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        comment=comment,
        created_by=created_by,
        metadata=metadata or {},
    )
    
    # Send notifications after response-critical DB work: queue on commit so email/push
    # (SMTP, FCM, etc.) do not block the booking API.
    if send_notification:
        eid = event.event_id
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(lambda: _dispatch_booking_event_notification(eid))
        else:
            _dispatch_booking_event_notification(eid)

        from .print_3d_notifications import maybe_dispatch_print_3d_stl_notification

        maybe_dispatch_print_3d_stl_notification(
            booking,
            event_type,
            previous_status=previous_status,
            new_status=new_status,
            metadata=metadata,
        )

    # Print_3D cleanup on completion should happen even when email notifications are suppressed
    # (e.g. auto-complete when sample lifecycle becomes Analyzed).
    from .print_3d_notifications import maybe_dispatch_print_3d_stl_cleanup

    maybe_dispatch_print_3d_stl_cleanup(
        booking,
        event_type,
        new_status=new_status,
    )

    return event


def _dispatch_booking_event_notification(event_id: int) -> None:
    """
    Send booking email/push off the request path (daemon thread).

    Sends directly in-thread rather than only queuing Celery. Celery ``.delay()``
    can succeed (message accepted by Redis) while no worker delivers mail, so
    confirmations were silently dropped on production. The HTTP request still
    returns immediately because this runs in a background thread.
    """

    def _run():
        from django.db import close_old_connections

        close_old_connections()
        try:
            event = BookingEvent.objects.select_related(
                "booking",
                "booking__user",
                "booking__equipment",
                "created_by",
            ).get(event_id=event_id)
            send_booking_event_notification(event)
            BookingEvent.objects.filter(event_id=event_id).update(notification_sent=True)
        except Exception:
            logger.error(
                "Background booking notification failed for event_id=%s",
                event_id,
                exc_info=True,
            )
            # Last-resort: try Celery if in-thread send failed (e.g. transient SMTP).
            try:
                from iic_booking.equipment.tasks import send_booking_event_notifications_task

                send_booking_event_notifications_task.delay(event_id)
            except Exception:
                logger.error(
                    "Also failed to queue Celery booking notification for event_id=%s",
                    event_id,
                    exc_info=True,
                )
        finally:
            close_old_connections()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"booking-notify-{event_id}",
    ).start()


def send_booking_event_notification(event: BookingEvent) -> None:
    """
    Send email and push notifications for a booking event.
    
    Args:
        event: BookingEvent instance
    """
    if not event or not event.booking or not event.booking.user:
        logger.warning("Invalid event, booking, or user for notification")
        return
    
    user = event.booking.user
    booking = event.booking
    equipment = booking.equipment
    
    # Determine notification type and template codes
    notification_type = "info"
    email_template_code = None
    push_template_code = None
    
    if event.event_type == BookingEventType.CREATED:
        notification_type = "info"
        _md = event.metadata or {}
        if _md.get("from_waitlist"):
            email_template_code = "booking_waitlist_confirmed_email"
        elif _md.get("repeat_sample_from_request"):
            email_template_code = "repeat_sample_booking_confirmed_email"
        else:
            email_template_code = "booking_created_email"
        push_template_code = "booking_created_push"
    elif event.event_type == BookingEventType.CANCELLED:
        notification_type = "warning"
        email_template_code = "booking_cancelled_email"
        push_template_code = "booking_cancelled_push"
    elif event.event_type == BookingEventType.RESCHEDULED:
        notification_type = "info"
        email_template_code = "booking_rescheduled_email"
        push_template_code = "booking_rescheduled_push"
    elif event.event_type == BookingEventType.CONFIRMED:
        notification_type = "info"
        email_template_code = "booking_confirmed_email"
        push_template_code = "booking_confirmed_push"
    elif event.event_type == BookingEventType.COMPLETED:
        notification_type = "info"
        email_template_code = "booking_completed_email"
        push_template_code = "booking_completed_push"
    elif event.event_type == BookingEventType.REFUNDED:
        notification_type = "info"
        email_template_code = "booking_refunded_email"
        push_template_code = "booking_refunded_push"
    elif event.event_type == BookingEventType.ABSENT:
        notification_type = "warning"
        email_template_code = "operator_unavailable_email"
        push_template_code = "booking_absent_push"
    elif event.event_type == BookingEventType.STATUS_CHANGED:
        notification_type = "info"
        _md = event.metadata or {}
        if _md.get("urgent_hold_converted"):
            email_template_code = "urgent_booking_hold_confirmed_email"
        elif _md.get("urgent_hold_released"):
            email_template_code = "urgent_booking_hold_released_email"
        else:
            email_template_code = "booking_status_changed_email"
        push_template_code = "booking_status_changed_push"
    elif event.event_type == BookingEventType.COMMENT:
        notification_type = "info"
        email_template_code = "booking_comment_email"
        push_template_code = "booking_comment_push"
    elif event.event_type == BookingEventType.CHARGE_RECALCULATED:
        notification_type = "info"
        email_template_code = "booking_charge_recalculated_email"
        push_template_code = "booking_charge_recalculated_push"
    elif event.event_type == BookingEventType.REPEAT_SAMPLE_OFFERED:
        notification_type = "info"
        email_template_code = "booking_comment_email"
        push_template_code = "booking_comment_push"
    elif event.event_type == BookingEventType.REPEAT_SAMPLE_CREATED:
        notification_type = "info"
        email_template_code = "repeat_sample_booking_confirmed_email"
        push_template_code = "booking_created_push"
    
    # Prepare template context (use virtual / display id for any user-visible booking reference)
    display_booking_ref = booking_display_id_for_email(booking)
    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "booked_for_user_name": user.name or user.email,
        "booked_for_user_email": user.email or "",
        "booking_id": display_booking_ref,
        "virtual_booking_id": display_booking_ref,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "event_type": event.get_event_type_display(),
        "comment": event.comment or "No comment",
        "event_date": event.created_at.strftime("%Y-%m-%d %H:%M:%S") if event.created_at else "",
        "user_sample_preparation_notice": "",
        "user_sample_preparation_notice_html": "",
        "equipment_booking_email_extra": "",
        "equipment_booking_email_extra_html": "",
        "equipment_completion_email_extra": "",
        "equipment_completion_email_extra_html": "",
    }
    
    # Add status information if available
    if event.previous_status:
        context["previous_status"] = dict(BookingStatus.choices).get(event.previous_status, event.previous_status)
    if event.new_status:
        context["new_status"] = dict(BookingStatus.choices).get(event.new_status, event.new_status)

    # For booking confirmation (CREATED or CONFIRMED from hold): include final wallet balance so we don't send a separate wallet debit email
    # Also when an urgent hold is converted to BOOKED (wallet may have been debited).
    _md_wallet = event.metadata or {}
    if event.event_type in (BookingEventType.CREATED, BookingEventType.CONFIRMED) or (
        event.event_type == BookingEventType.STATUS_CHANGED and _md_wallet.get("urgent_hold_converted")
    ):
        try:
            from iic_booking.users.repositories.wallet_repository import WalletRepository
            wallet_target, _ = WalletRepository.get_booking_wallet_target(
                user, getattr(equipment, "internal_department", None)
            )
            if wallet_target is not None:
                wallet_target.refresh_from_db()
                context["wallet_balance_after"] = f"₹{wallet_target.balance:.2f}"
            else:
                context["wallet_balance_after"] = "N/A"
        except Exception:
            context["wallet_balance_after"] = "N/A"
    
    # Add metadata to context
    if event.metadata:
        context.update(event.metadata)
    if email_template_code == "booking_waitlist_confirmed_email":
        context.setdefault("waitlist_joined_at_display", "—")
        context.setdefault("waitlist_position", "—")
    context.update({
        "total_charge": str(booking.total_charge),
        "total_time_minutes": str(booking.total_time_minutes),
        "total_hours": str(round(booking.total_time_minutes / 60, 2)) if booking.total_time_minutes else "0",
    })
    # Charge breakdown for recalculated emails (list of {description, amount})
    if event.event_type == BookingEventType.CHARGE_RECALCULATED and event.metadata and "charge_breakdown" in event.metadata:
        context["charge_breakdown"] = event.metadata["charge_breakdown"]
    elif booking.charge_breakdown:
        context["charge_breakdown"] = booking.charge_breakdown
    # Preformatted charge breakdown text for email body (template uses simple {{ var }} substitution)
    if event.event_type == BookingEventType.CHARGE_RECALCULATED:
        breakdown = context.get("charge_breakdown") or []
        if isinstance(breakdown, list):
            lines = []
            for line in breakdown:
                desc = line.get("description", "")
                amt = line.get("amount", 0)
                try:
                    amt = float(amt)
                except (TypeError, ValueError):
                    amt = 0
                lines.append(f"  {desc}: ₹{amt:.2f}")
            context["charge_breakdown_text"] = "\n".join(lines) if lines else "—"
        else:
            context["charge_breakdown_text"] = "—"
        context.setdefault("refund_amount", event.metadata.get("refund_amount") if event.metadata else "")
        context.setdefault("extra_amount", event.metadata.get("extra_amount") if event.metadata else "")
    
    # Get start and end times from slots (for email/display)
    # When equipment has Hide time (SLOT_ID): for non-admin/OIC recipients show date only and hide duration; admin/OIC always get full time.
    daily_slots = booking.daily_slots.select_related('slot_master').all().order_by('start_datetime')
    from iic_booking.users.models.user_type import UserType
    recipient_is_admin_oic = getattr(user, 'user_type', None) in UserType.get_admin_panel_codes()
    use_slot_id_display = (
        equipment
        and getattr(equipment, 'weekly_view_display', None) == 'SLOT_ID'
        and not recipient_is_admin_oic
    )

    if daily_slots.exists():
        def _format_local(dt, fmt):
            if not dt:
                return ""
            try:
                dt_local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
            except Exception:
                dt_local = dt
            return dt_local.strftime(fmt)

        if use_slot_id_display:
            # Slot ID mode: send only date and slot ID(s), no exact timing or duration
            first_slot = daily_slots.first()
            booking_date_str = _format_local(first_slot.start_datetime, "%Y-%m-%d") if first_slot.start_datetime else ""
            slot_parts = []
            for ds in daily_slots:
                if ds.slot_master:
                    name = ds.slot_master.slot_name or f"Slot {ds.slot_master.slot_number}"
                    slot_parts.append(name.strip() or f"Slot {ds.slot_master.slot_number}")
                else:
                    slot_parts.append("—")
            slot_id_display = ", ".join(slot_parts) if slot_parts else "—"
            context["start_time"] = f"Date: {booking_date_str}, Slot(s): {slot_id_display}"
            context["end_time"] = ""
            context["booking_date"] = booking_date_str
            context["slot_id_display"] = slot_id_display
            context["total_time_minutes"] = ""
            context["total_hours"] = ""
        else:
            context["start_time"] = _format_local(daily_slots.first().start_datetime, "%Y-%m-%d %H:%M:%S")
            context["end_time"] = _format_local(daily_slots.last().end_datetime, "%Y-%m-%d %H:%M:%S")
            context["booking_date"] = ""
            context["slot_id_display"] = ""
    else:
        context["start_time"] = ""
        context["end_time"] = ""
        context["booking_date"] = ""
        context["slot_id_display"] = ""

    # Repeat sample emails: original booking reference (admin-approved request passes this in metadata)
    if email_template_code == "repeat_sample_booking_confirmed_email" and not context.get("original_booking_id"):
        orig_id = None
        if getattr(booking, "source_booking_id", None):
            src = getattr(booking, "source_booking", None)
            if src is None:
                src = Booking.objects.filter(booking_id=booking.source_booking_id).first()
            if src:
                orig_id = booking_display_id_for_email(src)
        context["original_booking_id"] = orig_id or "—"
    
    # Absolute link for emails and in-app (relative links do not work in email clients)
    booking_link = get_frontend_absolute_url(f"/my-bookings?booking={display_booking_ref}")
    context["link"] = booking_link or f"/my-bookings?booking={display_booking_ref}"
    # Ensure templates always render a meaningful note line.
    context["comment"] = (str(context.get("comment", "")).strip() or "No comment")
    
    # Metadata for communication log
    metadata = {
        "booking_id": display_booking_ref,
        "real_booking_id": booking.booking_id,
        "booking_display_id": display_booking_ref,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "notification_type": notification_type,
        "link": context["link"],
    }
    
    # Send email notification
    if email_template_code:
        try:
            # Booking confirmation emails: always include sample submission/collection timing + results info.
            if email_template_code in {
                "booking_created_email",
                "booking_waitlist_confirmed_email",
                "booking_confirmed_email",
                "urgent_booking_hold_confirmed_email",
                "repeat_sample_booking_confirmed_email",
            }:
                apply_equipment_booking_email_extra_to_context(
                    context, equipment, also_append_to_comment=True
                )
                _append_confirmation_instructions_to_context(context, also_append_to_comment=True)
                apply_user_sample_preparation_notice_to_context(
                    context, user, equipment, also_append_to_comment=True
                )
            if email_template_code == "booking_completed_email":
                apply_equipment_completion_email_extra_to_context(
                    context, equipment, also_append_to_comment=True
                )
            CommunicationService.send_email(
                recipient=user,
                template=email_template_code,
                template_context=context,
                metadata=metadata,
                cc_emails=(
                    [event.created_by.email]
                    if (
                        event.created_by
                        and event.created_by.id != user.id
                        and (event.created_by.email or "").strip()
                        and event.event_type == BookingEventType.CREATED
                    )
                    else None
                ),
            )
            logger.info(f"Booking event email notification sent to {user.email} for event {event.event_id}")
        except Exception as e:
            logger.error(
                f"Failed to send booking event email to {user.email}: {str(e)}",
                exc_info=True
            )
    # For internal student: also send booking confirmation to the associated Wallet owner (Supervisor)
    if event.event_type == BookingEventType.CREATED and email_template_code:
        try:
            from iic_booking.users.repositories.wallet_repository import WalletRepository

            wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
                user, getattr(booking.equipment, "internal_department", None)
            )
            if has_wallet and wallet_target and getattr(wallet_target, "wallet", None):
                wallet_owner = wallet_target.wallet.user
                if wallet_owner and wallet_owner.id != user.id:
                    wallet_context = context.copy()
                    wallet_context["user_name"] = wallet_owner.name or wallet_owner.email
                    wallet_context["user_email"] = wallet_owner.email
                    CommunicationService.send_email(
                        recipient=wallet_owner,
                        template=email_template_code,
                        template_context=wallet_context,
                        metadata=metadata,
                    )
                    logger.info(
                        "Booking confirmation sent to wallet owner %s for booking %s (booked for %s)",
                        wallet_owner.email,
                        display_booking_ref,
                        user.email,
                    )
        except Exception as e:
            logger.error(
                f"Failed to send booking confirmation to wallet owner: {str(e)}",
                exc_info=True,
            )
    # For charge recalculated: also send to Supervisor (if different from booking user) with same details and breakup
    if event.event_type == BookingEventType.CHARGE_RECALCULATED and email_template_code:
        try:
            from iic_booking.users.repositories.wallet_repository import WalletRepository
            wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
                user, getattr(booking.equipment, "internal_department", None)
            )
            if has_wallet and wallet_target and getattr(wallet_target, "wallet", None):
                wallet_owner = wallet_target.wallet.user
                if wallet_owner and wallet_owner.id != user.id:
                    wallet_context = context.copy()
                    wallet_context["user_name"] = wallet_owner.name or wallet_owner.email
                    wallet_context["user_email"] = wallet_owner.email
                    CommunicationService.send_email(
                        recipient=wallet_owner,
                        template=email_template_code,
                        template_context=wallet_context,
                        metadata=metadata,
                    )
                    logger.info(
                        "Charge recalculated email sent to Supervisor %s for booking %s",
                        wallet_owner.email,
                        display_booking_ref,
                    )
        except Exception as e:
            logger.error(
                f"Failed to send charge recalculated email to Supervisor: {str(e)}",
                exc_info=True,
            )
    # When hold is converted to booked (urgent request approved): also send confirmation to Supervisor
    if (
        event.event_type == BookingEventType.STATUS_CHANGED
        and email_template_code
        and getattr(event, "previous_status", None) == BookingStatus.HOLD
        and getattr(event, "new_status", None) == BookingStatus.BOOKED
    ):
        try:
            from iic_booking.users.repositories.wallet_repository import WalletRepository
            wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
                user, getattr(booking.equipment, "internal_department", None)
            )
            if has_wallet and wallet_target and getattr(wallet_target, "wallet", None):
                wallet_owner = wallet_target.wallet.user
                if wallet_owner and wallet_owner.id != user.id:
                    wallet_context = context.copy()
                    wallet_context["user_name"] = wallet_owner.name or wallet_owner.email
                    wallet_context["user_email"] = wallet_owner.email
                    CommunicationService.send_email(
                        recipient=wallet_owner,
                        template=email_template_code,
                        template_context=wallet_context,
                        metadata=metadata,
                    )
                    logger.info(
                        "Booking confirmation (hold converted) sent to Supervisor %s for booking %s",
                        wallet_owner.email,
                        display_booking_ref,
                    )
        except Exception as e:
            logger.error(
                f"Failed to send booking confirmation to Supervisor: {str(e)}",
                exc_info=True,
            )
    
    # Send push notification
    if push_template_code:
        try:
            # Prepare fallback title and message (use string codes so we always match DB values)
            _em = event.metadata or {}
            _ev = str(getattr(event, "event_type", "") or "")
            if _ev == BookingEventType.REPEAT_SAMPLE_CREATED.value or (
                _ev == BookingEventType.CREATED.value and _em.get("repeat_sample_from_request")
            ):
                title = "Repeat Sample Booking Confirmed"
                message = f"{display_booking_ref} — {equipment.name}: your repeat booking is confirmed."
            elif _ev == BookingEventType.REPEAT_SAMPLE_OFFERED.value:
                title = "Repeat Sample Request Approved"
                message = (
                    f"{display_booking_ref} — {equipment.name}: you may book one complimentary repeat sample "
                    "from My Bookings (same parameters, no charge)."
                )
            elif _ev == BookingEventType.STATUS_CHANGED.value and _em.get("urgent_hold_converted"):
                title = "Urgent booking approved"
                message = f"{display_booking_ref} — {equipment.name}: hold confirmed as booking."
            elif _ev == BookingEventType.STATUS_CHANGED.value and _em.get("urgent_hold_released"):
                title = "Urgent booking update"
                message = f"{display_booking_ref} — {equipment.name}: hold released."
            elif _ev == BookingEventType.CREATED.value and _em.get("from_waitlist"):
                title = "Waitlist Confirmed"
                joined = (_em.get("waitlist_joined_at_display") or "").strip()
                message = f"{display_booking_ref} — {equipment.name}: your waitlisted booking is confirmed."
                if joined:
                    message += f" Waitlist request date and time: {joined}."
            else:
                title = f"Booking — {event.get_event_type_display()}"
                message = f"Booking #{display_booking_ref} for {equipment.name}"
                if event.comment:
                    message += f": {event.comment}"
                elif event.new_status:
                    message += f" - Status changed to {context.get('new_status', event.new_status)}"
            
            CommunicationService.send_push_notification(
                recipient=user,
                template=push_template_code,
                template_context=context,
                metadata=metadata,
                title=title,
                message=message,
            )
            logger.info(f"Booking event push notification sent to {user.email} for event {event.event_id}")
        except Exception as e:
            logger.error(
                f"Failed to send booking event push to {user.email}: {str(e)}",
                exc_info=True
            )
