"""Email the Officer(s) in charge about Type B urgent booking requests.

Sent once when the request is raised and once more when the supervisor approves it (ready to allocate).
"""

from __future__ import annotations

import logging

from iic_booking.communication.email_branding import absolute_http_url
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.users.display import get_user_display_name

from .models import UrgentBookingRequestStatus
from .models import UrgentBookingRequestType

logger = logging.getLogger(__name__)

EMAIL_TEMPLATE_CODE = "urgent_booking_request_oic_alert_email"
STAGE_SUBMITTED = "submitted"
STAGE_SUPERVISOR_APPROVED = "supervisor_approved"


def urgent_request_path(request_id: int) -> str:
    return f"/urgent-requests?request={request_id}"


def _duration_text(minutes) -> str:
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def _required_minutes(req):
    if req.duration_minutes:
        return req.duration_minutes
    hold = req.hold_booking
    return getattr(hold, "total_time_minutes", None) if hold else None


def _amount(req):
    if req.estimated_charge is not None:
        return req.estimated_charge
    hold = req.hold_booking
    return getattr(hold, "total_charge", None) if hold else None


def _status_line(req, stage: str) -> tuple[str, str]:
    if stage == STAGE_SUPERVISOR_APPROVED:
        return (
            "Ready to allocate",
            "The supervisor approved this request. It is now ready for you to allocate slots (or reject).",
        )
    if req.pending_supervisor_approval:
        return (
            "Awaiting supervisor approval",
            "It is waiting for the requester's supervisor to approve it. You will get another email once it is "
            "ready to allocate.",
        )
    if req.requires_slot_allocation:
        return "Ready to allocate", "It is ready for you to choose the date and slots (or reject)."
    return "Ready for review", "It is ready for you to approve, reject or reschedule."


def send_urgent_oic_alert_emails(req, *, stage: str) -> int:
    """Email every OIC and active temporary OIC of the equipment (never the requester). Returns emails sent."""
    from .booking_lab_messages import ensure_email_template
    from .reports import get_equipment_oic_users

    if req.request_type != UrgentBookingRequestType.REVIEWER_URGENT or req.status != UrgentBookingRequestStatus.PENDING:
        return 0
    equipment = req.equipment
    recipients = [u for u in get_equipment_oic_users(equipment) if u.id != req.user_id and (u.email or "").strip()]
    if not recipients:
        return 0
    try:
        ensure_email_template(EMAIL_TEMPLATE_CODE)
    except Exception:
        logger.exception("Urgent OIC alert template unavailable request_id=%s", req.id)
        return 0

    requester = req.user
    status_label, status_detail = _status_line(req, stage)
    amount = _amount(req)
    path = urgent_request_path(req.id)
    if stage == STAGE_SUPERVISOR_APPROVED:
        headline = "Urgent booking request ready to allocate"
        intro = (
            f"The supervisor approved urgent (Type B) request #{req.id} for {equipment.name}. "
            "It is now waiting for you."
        )
    else:
        headline = "New urgent booking request"
        intro = f"A new urgent (Type B) booking request #{req.id} was raised for {equipment.name}. {status_detail}"
    context = {
        "headline": headline,
        "intro": intro,
        "request_id": req.id,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "requester_name": get_user_display_name(requester),
        "requester_category": str(requester.get_user_type_display_label() or ""),
        "required_time": _duration_text(_required_minutes(req)),
        "amount": f"₹{amount:,.2f} (includes the 50% urgent surcharge)" if amount is not None else "",
        "preferred_schedule": (req.preferred_schedule or "").strip(),
        "reason": (req.reviewer_comment or "").strip(),
        "status_label": status_label,
        "status_detail": status_detail,
        "link": absolute_http_url(get_frontend_absolute_url(path) or path),
    }
    metadata = {"urgent_booking_request_id": req.id, "oic_alert_stage": stage}
    sent = 0
    for oic in recipients:
        try:
            CommunicationService.send_email(
                recipient=oic,
                template=EMAIL_TEMPLATE_CODE,
                template_context={**context, "user_name": get_user_display_name(oic)},
                metadata=metadata,
            )
            sent += 1
        except Exception:
            logger.exception("Failed urgent OIC alert email request_id=%s user_id=%s", req.id, oic.id)
    return sent
