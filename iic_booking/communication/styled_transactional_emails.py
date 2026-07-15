"""Styled HTML emails for wallet and booking workflows."""

from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.utils.html import escape

from iic_booking.communication.utils import (
    get_frontend_absolute_url,
    get_backend_absolute_url,
    booking_display_id_for_email,
)


def _shell(title: str, subtitle: str, body_html: str) -> str:
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f4f6fb;font-family:Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
      <tr><td align="center" style="padding:24px 12px;">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" style="max-width:640px;width:100%;">
          <tr>
            <td style="padding:24px;border-radius:16px 16px 0 0;background:#1d4ed8;color:#fff;">
              <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.95;">IIC Booking • IIT Roorkee</div>
              <div style="font-size:24px;font-weight:700;margin-top:8px;">{escape(title)}</div>
              <div style="font-size:14px;margin-top:6px;opacity:.95;">{escape(subtitle)}</div>
            </td>
          </tr>
          <tr><td style="background:#fff;padding:22px;border-radius:0 0 16px 16px;">{body_html}</td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _send(recipient_email: str, subject: str, text_message: str, html_message: str) -> None:
    from iic_booking.users.test_accounts import redirect_email_address

    delivery_email, subject = redirect_email_address(recipient_email, subject=subject)
    send_mail(
        subject=subject,
        message=text_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[delivery_email],
        html_message=html_message,
        fail_silently=False,
    )


def _signed_wallet_join_action_link(join_request, action: str) -> str:
    payload = {
        "request_id": int(join_request.id),
        "faculty_id": int(join_request.faculty_id),
        "action": str(action).lower(),
    }
    token = signing.dumps(payload, salt="wallet-join-email-action")
    return get_backend_absolute_url(
        f"/api/wallet/join-requests/{join_request.id}/email-action/{action}/?token={token}"
    )


def send_wallet_join_request_submitted_emails(join_request) -> None:
    """Send colorful wallet-join request emails to faculty and student."""
    student = join_request.student
    faculty = join_request.faculty
    wallet_link = get_frontend_absolute_url(f"/wallet?join_request_id={join_request.id}")
    approve_link = _signed_wallet_join_action_link(join_request, "approve")
    reject_link = _signed_wallet_join_action_link(join_request, "reject")

    faculty_body = (
        f"<p style='margin:0 0 12px 0;'>A student has requested to join your wallet.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Student:</b> {escape(student.name or student.email)} ({escape(student.email)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Message:</b> {escape(join_request.message or 'No message')}</p>"
        f"<p style='margin:16px 0 0 0;'>"
        f"<a href='{escape(approve_link)}' style='display:inline-block;background:#16a34a;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;margin-right:8px;'>Approve</a>"
        f"<a href='{escape(reject_link)}' style='display:inline-block;background:#dc2626;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Reject</a>"
        f"</p>"
        f"<p style='margin:12px 0 0 0;'><a href='{escape(wallet_link)}' style='color:#2563eb;text-decoration:none;'>Open wallet request page</a></p>"
    )
    faculty_html = _shell("Wallet Join Request", "Action needed from faculty", faculty_body)
    faculty_text = (
        f"Wallet join request received.\nStudent: {student.name or student.email} ({student.email})\n"
        f"Message: {join_request.message or 'No message'}\nApprove: {approve_link}\nReject: {reject_link}\nReview: {wallet_link}"
    )
    _send(faculty.email, "[IIC Booking] New wallet join request", faculty_text, faculty_html)

    student_body = (
        f"<p style='margin:0 0 12px 0;'>Your request has been sent to the faculty wallet owner.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Faculty:</b> {escape(faculty.name or faculty.email)} ({escape(faculty.email)})</p>"
        f"<p style='margin:16px 0 0 0;'><a href='{escape(wallet_link)}' style='background:#111827;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Open Wallet Requests</a></p>"
    )
    student_html = _shell("Request Submitted", "Wallet join request created successfully", student_body)
    student_text = f"Your wallet join request was submitted successfully.\nFaculty: {faculty.name or faculty.email}\nView status: {wallet_link}"
    _send(student.email, "[IIC Booking] Wallet join request submitted", student_text, student_html)


def send_wallet_join_request_decision_email(join_request, action: str) -> None:
    """Send colorful decision email to student after approve/reject/remove/cancel."""
    student = join_request.student
    faculty = join_request.faculty
    wallet_link = get_frontend_absolute_url("/wallet")
    action_label = action.strip().lower()
    pretty = "Approved" if action_label == "approved" else "Rejected" if action_label == "rejected" else "Updated"
    body = (
        f"<p style='margin:0 0 12px 0;'>Your wallet join request has been <b>{pretty}</b>.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Faculty:</b> {escape(faculty.name or faculty.email)} ({escape(faculty.email)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Response:</b> {escape(join_request.faculty_response or 'No additional remarks')}</p>"
        f"<p style='margin:16px 0 0 0;'><a href='{escape(wallet_link)}' style='background:#2563eb;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Open Wallet</a></p>"
    )
    html = _shell("Wallet Request Update", "Status changed", body)
    text = (
        f"Wallet request status: {pretty}\nFaculty: {faculty.name or faculty.email} ({faculty.email})\n"
        f"Response: {join_request.faculty_response or 'No additional remarks'}\nOpen wallet: {wallet_link}"
    )
    _send(student.email, f"[IIC Booking] Wallet request {pretty.lower()}", text, html)


def send_booking_created_email_to_recipient(booking, recipient, booked_for_user) -> None:
    """Send colorful booking-created summary email to a recipient."""
    display_id = booking_display_id_for_email(booking)
    link = get_frontend_absolute_url(f"/my-bookings?booking={display_id}")
    body = (
        f"<p style='margin:0 0 12px 0;'>A new booking has been created.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Booking ID:</b> {escape(display_id)}</p>"
        f"<p style='margin:0 0 8px 0;'><b>Equipment:</b> {escape(booking.equipment.name)} ({escape(booking.equipment.code)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Booked for:</b> {escape(booked_for_user.name or booked_for_user.email)} ({escape(booked_for_user.email)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Total charge:</b> Rs {escape(str(booking.total_charge))}</p>"
        f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:#111827;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>View Booking</a></p>"
    )
    html = _shell("Booking Confirmed", "Your booking details are ready", body)
    text = (
        f"Booking confirmed.\nBooking ID: {display_id}\nEquipment: {booking.equipment.name} ({booking.equipment.code})\n"
        f"Booked for: {booked_for_user.name or booked_for_user.email} ({booked_for_user.email})\n"
        f"Total charge: Rs {booking.total_charge}\nView: {link}"
    )
    _send(recipient.email, f"[IIC Booking] Booking confirmed #{display_id}", text, html)


def send_return_shipping_tracking_email(*, booking, carrier: str, tracking_number: str) -> None:
    """Notify external user that return shipping has been dispatched with carrier and tracking."""
    user = getattr(booking, "user", None)
    if not user or not getattr(user, "email", None):
        return
    display_id = booking_display_id_for_email(booking)
    link = get_frontend_absolute_url(f"/my-bookings?booking={display_id}")
    carrier_line = (carrier or "").strip() or "—"
    tracking = (tracking_number or "").strip() or "—"
    body = (
        f"<p style='margin:0 0 12px 0;'>Your samples have been dispatched for return shipment.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Booking ID:</b> {escape(display_id)}</p>"
        f"<p style='margin:0 0 8px 0;'><b>Equipment:</b> {escape(booking.equipment.name)} ({escape(booking.equipment.code)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Shipping company:</b> {escape(carrier_line)}</p>"
        f"<p style='margin:0 0 8px 0;'><b>Tracking number:</b> {escape(tracking)}</p>"
        f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:#2563eb;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>View booking details</a></p>"
    )
    html = _shell("Return Shipment Update", "Tracking information available", body)
    text = (
        f"Return shipment dispatched.\nBooking ID: {display_id}\nEquipment: {booking.equipment.name} ({booking.equipment.code})\n"
        f"Shipping company: {carrier_line}\nTracking number: {tracking}\nView booking: {link}"
    )
    _send(user.email, f"[IIC Booking] Return shipment tracking for #{display_id}", text, html)


def send_wallet_recharge_approved_faculty_email(recharge_request) -> None:
    """Notify faculty wallet owner when a student's recharge is approved."""
    wallet_owner = getattr(getattr(recharge_request, "wallet", None), "user", None)
    requester = recharge_request.user
    if not wallet_owner or wallet_owner.id == requester.id:
        return

    link = get_frontend_absolute_url(f"/wallet/recharge-requests/{recharge_request.id}")
    body = (
        f"<p style='margin:0 0 12px 0;'>A recharge request linked to your wallet has been approved.</p>"
        f"<p style='margin:0 0 8px 0;'><b>Requested by:</b> {escape(requester.name or requester.email)} ({escape(requester.email)})</p>"
        f"<p style='margin:0 0 8px 0;'><b>Amount:</b> Rs {escape(str(recharge_request.amount))}</p>"
        f"<p style='margin:0 0 8px 0;'><b>Department:</b> {escape(recharge_request.department.name if recharge_request.department else 'N/A')}</p>"
        f"<p style='margin:16px 0 0 0;'><a href='{escape(link)}' style='background:#2563eb;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;'>Open Recharge Request</a></p>"
    )
    html = _shell("Recharge Approved", "Faculty wallet notification", body)
    text = (
        f"Recharge approved for your wallet.\nRequested by: {requester.name or requester.email} ({requester.email})\n"
        f"Amount: Rs {recharge_request.amount}\nDepartment: {recharge_request.department.name if recharge_request.department else 'N/A'}\n"
        f"Link: {link}"
    )
    _send(wallet_owner.email, "[IIC Booking] Wallet recharge approved", text, html)

