"""Results inbox (per-user viewed state) and internal research-data sharing."""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import Q, QuerySet
from django.utils.html import escape

from iic_booking.equipment.models import Booking, BookingDataShare, BookingResultView, DynamicInputField
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

INTERNAL_STUDENT_TYPES = (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT)


def is_internal_iitr_user(user) -> bool:
    """Active IIT Roorkee student, or active faculty of an internal department."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.user_type in INTERNAL_STUDENT_TYPES:
        return True
    if user.user_type == UserType.FACULTY:
        department = getattr(user, "department", None)
        return bool(department and department.department_type == DepartmentType.INTERNAL)
    return False


def internal_iitr_users() -> QuerySet:
    from django.contrib.auth import get_user_model

    return (
        get_user_model()
        .objects.filter(is_active=True)
        .filter(
            Q(user_type__in=INTERNAL_STUDENT_TYPES)
            | Q(user_type=UserType.FACULTY, department__department_type=DepartmentType.INTERNAL)
        )
        .select_related("department")
    )


def active_shares_for_booking(booking: Booking) -> QuerySet:
    return (
        BookingDataShare.objects.filter(booking=booking, revoked_at__isnull=True)
        .select_related("shared_with", "shared_with__department", "shared_by")
    )


def is_active_share_recipient(user, booking: Booking) -> bool:
    if not is_internal_iitr_user(user):
        return False
    return BookingDataShare.objects.filter(
        booking=booking, shared_with=user, revoked_at__isnull=True
    ).exists()


def mark_results_viewed(user, booking: Booking) -> None:
    """Record a results download for the owner or a share recipient (staff downloads are not tracked)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return
    if booking.user_id != user.pk and not is_active_share_recipient(user, booking):
        return
    try:
        view, created = BookingResultView.objects.get_or_create(booking=booking, user=user)
        if not created:
            view.save(update_fields=["last_viewed_at"])
    except Exception:
        logger.exception("mark_results_viewed failed booking=%s user=%s", booking.pk, user.pk)


def user_share_summary(user) -> dict[str, Any]:
    department = getattr(user, "department", None)
    return {
        "id": user.pk,
        "name": user.name or user.email,
        "email": user.email,
        "department": department.name if department else None,
    }


def user_share_details(user) -> dict[str, Any]:
    """Details shown to the sharer before confirming (phone intentionally excluded)."""
    data = user_share_summary(user)
    department = getattr(user, "department", None)
    data.update(
        {
            "user_type": user.user_type,
            "user_type_label": user.get_user_type_display_label() or user.user_type,
            "department_code": department.code if department else None,
            "id_number": user.emp_id or None,
            "designation": user.designation or None,
            "degree_name": user.degree_name or None,
            "branch_name": user.branch_name or None,
            "profile_picture": user.profile_picture.url if user.profile_picture else None,
        }
    )
    return data


def booking_input_summary(booking: Booking) -> list[dict[str, Any]]:
    """Booking input values with their configured labels, for shared-data views."""
    values = booking.input_values or {}
    if not isinstance(values, dict) or not values:
        return []
    labels = dict(
        DynamicInputField.objects.filter(equipment_id=booking.equipment_id).values_list("field_key", "field_label")
    )
    summary = []
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = ", ".join(f"{k}: {v}" for k, v in value.items())
        summary.append({"key": key, "label": labels.get(key) or key, "value": str(value)})
    return summary


def notify_share_recipient(share: BookingDataShare) -> None:
    from iic_booking.communication.email_branding import COLOR_PRIMARY, user_display_name
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.styled_transactional_emails import _send, _shell
    from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url

    booking = share.booking
    owner = share.shared_by
    recipient = share.shared_with
    display_id = booking_display_id_for_email(booking)
    equipment_name = booking.equipment.name if booking.equipment_id else ""
    link = get_frontend_absolute_url("/shared-data")
    owner_name = user_display_name(owner)
    message = f"{owner_name} shared the research data of booking {display_id} ({equipment_name}) with you."

    try:
        CommunicationService.send_push_notification(
            recipient=recipient,
            title="Research data shared with you",
            message=message,
            metadata={"notification_type": "info", "link": link, "booking_data_share_id": share.pk},
        )
    except Exception:
        logger.exception("share push notification failed share=%s", share.pk)

    try:
        body = (
            f"<p style='margin:0 0 12px 0;'>{escape(message)}</p>"
            f"<p style='margin:0 0 8px 0;'><b>Booking:</b> {escape(display_id)}</p>"
            f"<p style='margin:0 0 8px 0;'><b>Equipment:</b> {escape(equipment_name)}</p>"
            f"<p style='margin:0 0 8px 0;'><b>Shared by:</b> {escape(owner_name)} ({escape(owner.email)})</p>"
            f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:{COLOR_PRIMARY};color:#fff;"
            f"padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>View shared data</a></p>"
        )
        text = f"{message}\nBooking: {display_id}\nEquipment: {equipment_name}\nView: {link}"
        _send(recipient.email, "Research data shared with you", text, _shell("Research data shared", display_id, body))
    except Exception:
        logger.exception("share email failed share=%s", share.pk)
