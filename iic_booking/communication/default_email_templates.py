"""
Default CommunicationTemplate email redesigns for the Institute Equipment Booking Portal.

Uses shared branding helpers from ``email_branding``. Intended as the single source of
truth for seed/sync of email template rows (not Django migrations).
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from iic_booking.communication.email_branding import (
    PRODUCT_NAME,
    booking_details_rows,
    branded_plain_footer,
    build_standard_email,
    details_card_html,
    greeting_html,
    optional_cta_block,
    optional_detail_row,
    optional_note_block,
    paragraph_html,
    wrap_email_html,
)

DEFAULT_EMAIL_TEMPLATE_CODES: list[str] = [
    "booking_created_email",
    "booking_confirmed_email",
    "booking_cancelled_email",
    "booking_rescheduled_email",
    "booking_completed_email",
    "booking_refunded_email",
    "booking_absent_email",
    "booking_status_changed_email",
    "booking_comment_email",
    "booking_reminder_email",
    "booking_charge_recalculated_email",
    "booking_not_utilized_email",
    "booking_not_utilized_wallet_owner_email",
    "booking_unsuccessful_waitlist_email",
    "booking_waitlist_confirmed_email",
    "waitlist_slots_available_email",
    "waitlist_short_notice_slot_available_email",
    "operator_unavailable_email",
    "sample_disposed_email",
    "sample_submission_deadline_reminder_email",
    "repeat_sample_booking_confirmed_email",
    "urgent_reviewer_pending_supervisor_email",
    "urgent_booking_request_submitted_user_email",
    "urgent_booking_supervisor_decision_user_email",
    "urgent_booking_hold_confirmed_email",
    "urgent_booking_hold_released_email",
    "urgent_booking_admin_decision_user_email",
    "wallet_credit_email",
    "wallet_debit_email",
    "wallet_recharge_approved_email",
    "wallet_recharge_rejected_email",
    "wallet_recharge_pending_email",
    "wallet_low_balance_email",
    "wallet_recharge_request_email",
    "wallet_recharge_sric_office_email",
    "wallet_credit_facility_expired_email",
    "wallet_recharge_credit_facility_activated_email",
    "wallet_join_request_submitted_email",
    "wallet_join_request_approved_email",
    "wallet_join_request_rejected_email",
    "wallet_join_request_cancelled_email",
    "wallet_join_request_removed_email",
    "registration_self_verification_email",
    "registration_verification_otp_email",
    "registration_approval_confirmation_email",
    "support_ticket_resolution_email",
    "admin_bulk_email",
    "oic_monthly_report",
    "ta_operating_nomination_call_email",
    "student_nomination_intimation_email",
    "nomination_approved_student_email",
    "nomination_rejected_student_email",
    "ta_duty_allocation_email",
    "operator_leave_submitted_operator_email",
    "operator_leave_submitted_oic_email",
    "operator_leave_approved_operator_email",
    "operator_leave_rejected_operator_email",
]


_BOOKING_COMMON_HELP = (
    "{{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, "
    "{{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ booking_date }}, "
    "{{ slot_id_display }}, {{ duration_display }}, {{ total_hours }}, {{ total_charge }}, "
    "{{ comment }}, {{ link }}"
)


def _pack(
    code: str,
    built: dict[str, str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    variable_help: Optional[str] = None,
) -> dict[str, Any]:
    """Merge build_standard_email output into a CommunicationTemplate-shaped dict."""
    return {
        "code": code,
        "name": name or built.get("name") or code,
        "communication_type": "email",
        "subject": built["subject"],
        "body_text": built["body_text"],
        "body_html": built["body_html"],
        "description": description if description is not None else built.get("description", ""),
        "variable_help": variable_help if variable_help is not None else built.get("variable_help", ""),
        "is_active": True,
    }


def _booking_email(
    *,
    code: str,
    title: str,
    subject: str,
    intro: str,
    description: str,
    include_wallet: bool = False,
    include_status: bool = False,
    include_payment: bool = False,
    include_duration: bool = True,
    include_charges: bool = True,
    note_vars: Optional[Sequence[tuple[str, str]]] = None,
    extra_html: str = "",
    extra_detail_rows: Optional[Sequence[str]] = None,
    cta_label: str = "View booking",
    variable_help: Optional[str] = None,
    name_var: str = "user_name",
) -> dict[str, Any]:
    rows = booking_details_rows(
        include_wallet=include_wallet,
        include_status=include_status,
        include_payment=include_payment,
        include_duration=include_duration,
        include_charges=include_charges,
    )
    if extra_detail_rows:
        rows = list(rows) + list(extra_detail_rows)
    notes = list(note_vars or (("comment", "Note"),))
    built = build_standard_email(
        title=title,
        subject=subject,
        intro=intro,
        detail_rows=rows,
        details_heading="Booking details",
        note_vars=notes,
        cta_label=cta_label,
        extra_html=extra_html,
        name_var=name_var,
        description=description,
        variable_help=variable_help or _BOOKING_COMMON_HELP,
    )
    return _pack(code, built, name=title, description=description, variable_help=variable_help)


def _wallet_detail_rows(*, include_booking: bool = True) -> list[str]:
    rows = [
        optional_detail_row("Amount", "amount"),
        optional_detail_row("Description", "description"),
        optional_detail_row("Department", "department_name"),
        optional_detail_row("Department code", "department_code"),
        optional_detail_row("New balance", "balance"),
        optional_detail_row("Transaction date", "transaction_date"),
        optional_detail_row("Request ID", "request_id"),
        optional_detail_row("Request date", "request_date"),
        optional_detail_row("Approved by", "approved_by_email"),
        optional_detail_row("Project details", "project_details"),
        optional_detail_row("Threshold", "threshold"),
    ]
    if include_booking:
        rows.extend(
            [
                optional_detail_row("Booking ID", "booking_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
            ]
        )
    return rows


def _wallet_email(
    *,
    code: str,
    title: str,
    subject: str,
    intro: str,
    description: str,
    note_vars: Optional[Sequence[tuple[str, str]]] = None,
    include_booking: bool = True,
    cta_label: str = "View wallet",
    variable_help: str = "",
    name_var: str = "user_name",
    extra_html: str = "",
    extra_detail_rows: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    rows = _wallet_detail_rows(include_booking=include_booking)
    if extra_detail_rows:
        rows = list(rows) + list(extra_detail_rows)
    built = build_standard_email(
        title=title,
        subject=subject,
        intro=intro,
        detail_rows=rows,
        details_heading="Transaction details",
        note_vars=note_vars or (),
        cta_label=cta_label,
        extra_html=extra_html,
        name_var=name_var,
        description=description,
        variable_help=variable_help,
    )
    return _pack(code, built, name=title, description=description, variable_help=variable_help)


def _simple_email(
    *,
    code: str,
    title: str,
    subject: str,
    intro: str,
    description: str,
    detail_rows: Optional[Sequence[str]] = None,
    details_heading: str = "Details",
    note_vars: Optional[Sequence[tuple[str, str]]] = None,
    cta_label: str = "View details",
    cta_var: str = "link",
    name_var: str = "user_name",
    variable_help: str = "",
    extra_html: str = "",
    subtitle: str = "",
) -> dict[str, Any]:
    built = build_standard_email(
        title=title,
        subject=subject,
        intro=intro,
        detail_rows=detail_rows,
        details_heading=details_heading,
        note_vars=note_vars or (),
        cta_label=cta_label,
        cta_var=cta_var,
        name_var=name_var,
        extra_html=extra_html,
        subtitle=subtitle,
        description=description,
        variable_help=variable_help,
    )
    return _pack(code, built, name=title, description=description, variable_help=variable_help)


def _custom_branded_email(
    *,
    code: str,
    name: str,
    title: str,
    subject: str,
    body_inner_html: str,
    body_text: str,
    description: str,
    variable_help: str,
    preheader: str = "",
    subtitle: str = "",
) -> dict[str, Any]:
    html = wrap_email_html(
        title=title,
        body_inner_html=body_inner_html,
        preheader=preheader or title,
        subtitle=subtitle,
    )
    text = body_text.rstrip() + "\n" + branded_plain_footer()
    return {
        "code": code,
        "name": name,
        "communication_type": "email",
        "subject": subject,
        "body_text": text,
        "body_html": html,
        "description": description,
        "variable_help": variable_help,
        "is_active": True,
    }


def _booking_templates() -> list[dict[str, Any]]:
    booking_notes = (("comment", "Note"),)
    reminder_notes = (
        ("comment", "Note"),
        ("user_sample_preparation_notice", "Sample preparation"),
        ("equipment_booking_email_extra", "Additional information"),
    )
    return [
        _booking_email(
            code="booking_created_email",
            title="Booking Confirmed",
            subject="Booking Confirmed – {{ equipment_name }}",
            intro="Your equipment booking has been created successfully.",
            description="Email sent when a new booking is created.",
            include_wallet=True,
            note_vars=booking_notes,
            variable_help=_BOOKING_COMMON_HELP + ", {{ wallet_balance_after }}",
        ),
        _booking_email(
            code="booking_confirmed_email",
            title="Booking Confirmed",
            subject="Booking Confirmed – {{ equipment_name }}",
            intro="Your equipment booking has been confirmed.",
            description="Email sent when a booking is confirmed (BOOKED).",
            include_wallet=True,
            note_vars=booking_notes,
            variable_help=_BOOKING_COMMON_HELP + ", {{ wallet_balance_after }}",
        ),
        _booking_email(
            code="booking_cancelled_email",
            title="Booking Cancelled",
            subject="Booking Cancelled – {{ equipment_name }}",
            intro="Your booking has been cancelled.",
            description="Email sent when a booking is cancelled.",
            include_charges=False,
            include_duration=False,
            note_vars=(("comment", "Reason"),),
        ),
        _booking_email(
            code="booking_rescheduled_email",
            title="Booking Rescheduled",
            subject="Booking Rescheduled – {{ equipment_name }}",
            intro="Your booking has been rescheduled. Please review the updated slot details below.",
            description="Email sent when a booking is rescheduled.",
            note_vars=booking_notes,
        ),
        _booking_email(
            code="booking_completed_email",
            title="Booking Completed",
            subject="Booking Completed – {{ equipment_name }}",
            intro="Your booking has been marked as completed.",
            description="Email sent when a booking is marked as completed.",
            note_vars=(
                ("comment", "Note"),
                ("equipment_booking_email_extra", "Additional information"),
            ),
            variable_help=_BOOKING_COMMON_HELP + ", {{ equipment_booking_email_extra }}",
        ),
        _booking_email(
            code="booking_refunded_email",
            title="Booking Refunded",
            subject="Booking Refunded – {{ equipment_name }}",
            intro="A refund has been processed for your booking.",
            description="Email sent when a booking is refunded.",
            note_vars=booking_notes,
            extra_detail_rows=[optional_detail_row("Refund amount", "refund_amount")],
            variable_help=_BOOKING_COMMON_HELP + ", {{ refund_amount }}",
        ),
        _booking_email(
            code="booking_absent_email",
            title="Marked Absent",
            subject="Marked Absent – {{ equipment_name }}",
            intro="Your booking has been marked as absent / not attended.",
            description="Email sent when a booking is marked absent.",
            note_vars=(("comment", "Reason"),),
        ),
        _booking_email(
            code="booking_status_changed_email",
            title="Booking Status Updated",
            subject="Booking Status Updated – {{ equipment_name }}",
            intro="The status of your booking has been updated.",
            description="Email sent when a booking status changes (generic).",
            include_status=True,
            note_vars=booking_notes,
            variable_help=_BOOKING_COMMON_HELP + ", {{ previous_status }}, {{ new_status }}",
        ),
        _booking_email(
            code="booking_comment_email",
            title="New Booking Comment",
            subject="New Comment – {{ equipment_name }}",
            intro="A new comment has been added to your booking.",
            description="Email sent when a comment is added to a booking.",
            include_duration=False,
            include_charges=False,
            note_vars=(("comment", "Comment"),),
        ),
        _booking_email(
            code="booking_reminder_email",
            title="Booking Reminder",
            subject="Booking Reminder – {{ equipment_name }}",
            intro="This is a reminder that your equipment booking is scheduled for today.",
            description=(
                "Same-day reminder for BOOKED bookings (scheduled daily). "
                "May include sample preparation and equipment-specific notices."
            ),
            note_vars=reminder_notes,
            variable_help=(
                _BOOKING_COMMON_HELP
                + ", {{ user_sample_preparation_notice }}, {{ user_sample_preparation_notice_html }}, "
                "{{ equipment_booking_email_extra }}, {{ equipment_booking_email_extra_html }}"
            ),
        ),
        _simple_email(
            code="booking_charge_recalculated_email",
            title="Charges Updated",
            subject="Booking Charges Updated – {{ equipment_name }}",
            intro=(
                "Your booking details were updated and the charges have been recalculated. "
                "If a refund is due, use Refund in the booking details. If an extra amount is due, use Pay Now."
            ),
            description="Sent when booking charges are recalculated after user input edit.",
            detail_rows=[
                optional_detail_row("Booking ID", "booking_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
                optional_detail_row("Previous charge", "previous_charge"),
                optional_detail_row("New charge", "new_charge"),
                optional_detail_row("Refund amount", "refund_amount"),
                optional_detail_row("Extra amount due", "extra_amount"),
            ],
            note_vars=(
                ("charge_breakdown_text", "Charge breakdown"),
                ("comment", "Note"),
            ),
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, "
                "{{ equipment_code }}, {{ previous_charge }}, {{ new_charge }}, "
                "{{ charge_breakdown_text }}, {{ refund_amount }}, {{ extra_amount }}, {{ comment }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="booking_not_utilized_email",
            title="Booking Not Utilized",
            subject="Booking Not Utilized – {{ equipment_name }}",
            intro=(
                "Your booking has been marked as <strong>Booking Not Utilized</strong> because the slot was not used. "
                "No refund will be issued. Please ensure optimum utilization of facility resources in future."
            ),
            description="Sent to the user when a booked slot is marked Booking Not Utilized. No refund.",
            detail_rows=[
                optional_detail_row("Booking ID", "booking_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Slot", "slot_details"),
            ],
            variable_help="{{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ slot_details }}, {{ booking_id }}, {{ link }}",
        ),
        _simple_email(
            code="booking_not_utilized_wallet_owner_email",
            title="Booking Not Utilized – Notice",
            subject="Booking Not Utilized – Wallet User Notice",
            intro=(
                "A booking made using your wallet has been marked as <strong>Booking Not Utilized</strong>. "
                "No refund has been issued. Please advise the user to utilize booked slots in future."
            ),
            description="Sent to the wallet owner/supervisor when a student's booking is marked Booking Not Utilized.",
            name_var="wallet_owner_name",
            detail_rows=[
                optional_detail_row("Student / user", "student_name"),
                optional_detail_row("Student email", "student_email"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Slot", "slot_details"),
                optional_detail_row("Booking ID", "booking_id"),
            ],
            variable_help=(
                "{{ wallet_owner_name }}, {{ student_name }}, {{ student_email }}, "
                "{{ equipment_name }}, {{ slot_details }}, {{ booking_id }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="booking_unsuccessful_waitlist_email",
            title="Added to Waitlist",
            subject="Booking Waitlisted – {{ equipment_name }}",
            intro=(
                "Your booking attempt was unsuccessful and you have been added to the waitlist. "
                "When slots become available you will be notified; allocation is first-come, first-served."
            ),
            description="Sent when a booking fails and the user is added to the equipment waitlist.",
            detail_rows=[
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
                optional_detail_row("Queue position", "waitlist_position"),
                optional_detail_row("Request time", "waitlist_joined_at_display"),
            ],
            note_vars=(("failure_reason", "Reason"),),
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}, "
                "{{ waitlist_position }}, {{ failure_reason }}, {{ waitlist_joined_at_display }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="booking_waitlist_confirmed_email",
            title="Waitlist Confirmed",
            subject="Waitlist Confirmed – {{ equipment_name }}",
            intro=(
                "Your waitlisted request has been confirmed. A slot became available and was assigned "
                "on a first-come, first-served basis."
            ),
            description="Sent when a waitlist entry is auto-confirmed and the wallet is debited.",
            detail_rows=[
                optional_detail_row("Booked for", "booked_for_user_name"),
                optional_detail_row("Booked-for email", "booked_for_user_email"),
                optional_detail_row("Waitlist request time", "waitlist_joined_at_display"),
                optional_detail_row("Queue position when joined", "waitlist_position"),
                *booking_details_rows(include_wallet=True),
            ],
            cta_label="Open booking",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ booked_for_user_name }}, {{ booked_for_user_email }}, "
                "{{ waitlist_joined_at_display }}, {{ waitlist_position }}, {{ booking_id }}, "
                "{{ virtual_booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, "
                "{{ end_time }}, {{ total_charge }}, {{ wallet_balance_after }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="waitlist_slots_available_email",
            title="Slots Available",
            subject="Slots Available – {{ equipment_name }}",
            intro=(
                "Slots have become available for equipment you are waitlisted on. "
                "Please log in promptly — allocation is first-come, first-served."
            ),
            description="Sent to waitlisted users when slots become available.",
            detail_rows=[
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
            ],
            cta_label="Book now",
            variable_help="{{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}, {{ link }}",
        ),
        _simple_email(
            code="waitlist_short_notice_slot_available_email",
            title="Short-Notice Slot Available",
            subject="Short-Notice Slot Available – {{ equipment_name }}",
            intro=(
                "A slot for <strong>{{ equipment_name }}</strong> is available at short notice "
                "(within the next {{ lead_hours }} hours). The system will not auto-allocate this slot. "
                "If your samples are ready, you may reserve it first-come, first-served. "
                "Cancellation or no-show after booking may attract applicable charges."
            ),
            description="Notifies waitlisted users of a short-notice slot that will not be auto-allocated.",
            detail_rows=[
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Date", "slot_date"),
                optional_detail_row("Time", "slot_time"),
                optional_detail_row("Lead window (hours)", "lead_hours"),
                optional_detail_row("Contact", "contact_line"),
            ],
            cta_label="Open booking portal",
            variable_help=(
                "{{ user_name }}, {{ equipment_name }}, {{ lead_hours }}, {{ slot_date }}, "
                "{{ slot_time }}, {{ contact_line }}, {{ link }}"
            ),
        ),
        _booking_email(
            code="operator_unavailable_email",
            title="Operator Unavailable",
            subject="Operator Unavailable – {{ equipment_name }}",
            intro=(
                "Your booking has been marked <strong>Operator Unavailable</strong>. "
                "A full refund has been issued to your wallet."
            ),
            description="Sent when a booking is marked Operator Unavailable (full refund).",
            include_charges=False,
            note_vars=(("comment", "Note"),),
            extra_detail_rows=[optional_detail_row("Refund amount", "refund_amount")],
            variable_help=_BOOKING_COMMON_HELP + ", {{ refund_amount }}",
        ),
        _simple_email(
            code="sample_disposed_email",
            title="Sample Disposed",
            subject="Sample Disposed – {{ equipment_name }}",
            intro=(
                "Your sample has been disposed after the retention period. "
                "If you believe this is an error, please contact the lab in-charge or OIC."
            ),
            description="Sent when lab/OIC marks a sample as DISPOSED after ARCHIVED.",
            detail_rows=[
                optional_detail_row("Booking ID", "booking_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Disposed at", "disposed_at"),
            ],
            note_vars=(("remarks", "Remarks"),),
            variable_help="{{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ booking_id }}, {{ disposed_at }}, {{ remarks }}",
        ),
        _simple_email(
            code="sample_submission_deadline_reminder_email",
            title="Sample Submission Deadline",
            subject="Sample Submission Deadline Approaching – {{ equipment_name }}",
            intro=(
                "Please submit your sample before <strong>{{ submission_deadline }}</strong> "
                "(about <strong>{{ remaining_label }}</strong> remaining). "
                "This notice is sent {{ advance_hours }} hour(s) before the deadline "
                "({{ lead_hours }} hour(s) before slot start)."
            ),
            description=(
                "Advance reminder when sample submission deadline is within 12 hours "
                "(deadline = slot start minus sample_submission_lead_hours)."
            ),
            detail_rows=[
                optional_detail_row("Booking ID", "booking_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
                optional_detail_row("Slot start", "start_time"),
                optional_detail_row("Submission deadline", "submission_deadline"),
                optional_detail_row("Time remaining", "remaining_label"),
            ],
            note_vars=(
                ("user_sample_preparation_notice", "Sample preparation"),
                ("equipment_booking_email_extra", "Additional information"),
            ),
            cta_label="View booking",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, "
                "{{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ submission_deadline }}, "
                "{{ lead_hours }}, {{ advance_hours }}, {{ remaining_label }}, {{ link }}, "
                "{{ user_sample_preparation_notice }}, {{ equipment_booking_email_extra }}"
            ),
        ),
        _booking_email(
            code="repeat_sample_booking_confirmed_email",
            title="Repeat Sample Approved",
            subject="Repeat Sample Accepted – {{ equipment_name }}",
            intro=(
                "Your <strong>repeat sample</strong> request has been approved. "
                "A complimentary repeat booking has been created."
            ),
            description="Sent when a repeat sample booking is created (admin-approved or user repeat flow).",
            include_wallet=True,
            note_vars=booking_notes,
            extra_detail_rows=[optional_detail_row("Original booking", "original_booking_id")],
            variable_help=_BOOKING_COMMON_HELP + ", {{ original_booking_id }}, {{ wallet_balance_after }}",
        ),
    ]


def _urgent_templates() -> list[dict[str, Any]]:
    return [
        _simple_email(
            code="urgent_reviewer_pending_supervisor_email",
            title="Supervisor Action Required",
            subject="Urgent Booking – Supervisor Action Required – {{ equipment_name }}",
            intro=(
                "<strong>{{ requester_name }}</strong> ({{ requester_email }}) has submitted an "
                "urgent booking request with reviewer evidence. Please approve or reject it in your "
                "supervisor queue before an administrator can review it."
            ),
            description="Sent to the wallet owner (supervisor) for REVIEWER_URGENT requests.",
            name_var="supervisor_name",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
            ],
            cta_label="Open supervisor queue",
            variable_help=(
                "{{ supervisor_name }}, {{ requester_name }}, {{ requester_email }}, "
                "{{ equipment_name }}, {{ equipment_code }}, {{ request_id }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="urgent_booking_request_submitted_user_email",
            title="Urgent Request Submitted",
            subject="Urgent Booking Request Submitted – {{ equipment_name }}",
            intro="Your urgent booking request has been submitted successfully.",
            description="Sent to the requester when an urgent booking request is submitted.",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
                optional_detail_row("Request type", "request_type_label"),
            ],
            note_vars=(("next_steps", "Next steps"),),
            cta_label="Track request",
            variable_help=(
                "{{ user_name }}, {{ request_id }}, {{ equipment_name }}, {{ equipment_code }}, "
                "{{ request_type_label }}, {{ next_steps }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="urgent_booking_supervisor_decision_user_email",
            title="Urgent Request – Supervisor Decision",
            subject="Urgent Booking Request {{ decision_phrase }} – {{ equipment_name }}",
            intro="{{ decision_summary }}",
            description="Sent when the supervisor approves or rejects a reviewer-urgent request.",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
                optional_detail_row("Supervisor", "supervisor_name"),
            ],
            note_vars=(
                ("supervisor_notes", "Supervisor notes"),
                ("next_steps", "Next steps"),
            ),
            variable_help=(
                "{{ user_name }}, {{ request_id }}, {{ equipment_name }}, {{ equipment_code }}, "
                "{{ decision_phrase }}, {{ decision_summary }}, {{ supervisor_name }}, "
                "{{ supervisor_notes }}, {{ next_steps }}, {{ link }}"
            ),
        ),
        _booking_email(
            code="urgent_booking_hold_confirmed_email",
            title="Urgent Hold Confirmed",
            subject="Urgent Booking Confirmed – {{ equipment_name }}",
            intro=(
                "Your urgent booking request was approved by the administrator. "
                "Your held slots are now a confirmed booking."
            ),
            description="Sent when an urgent request is approved and HOLD converts to BOOKED.",
            include_status=True,
            note_vars=(("comment", "Note"),),
            variable_help=_BOOKING_COMMON_HELP + ", {{ previous_status }}, {{ new_status }}",
        ),
        _booking_email(
            code="urgent_booking_hold_released_email",
            title="Urgent Hold Released",
            subject="Urgent Booking Hold Released – {{ equipment_name }}",
            intro=(
                "Your hold linked to an urgent booking request has been released. "
                "The booking is no longer active and the slots have been freed."
            ),
            description="Sent when a HOLD is released due to urgent request rejection or expiry.",
            include_status=True,
            include_charges=False,
            include_duration=False,
            note_vars=(("comment", "Note"),),
            variable_help=_BOOKING_COMMON_HELP + ", {{ previous_status }}, {{ new_status }}",
        ),
        _simple_email(
            code="urgent_booking_admin_decision_user_email",
            title="Urgent Request – Admin Decision",
            subject="{{ decision_headline }} – {{ equipment_name }}",
            intro="{{ decision_body }}",
            description="Sent when Admin/OIC approves or rejects an urgent request without a hold conversion email.",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("Equipment", "equipment_name"),
                optional_detail_row("Equipment code", "equipment_code"),
            ],
            note_vars=(("admin_notes", "Admin notes"),),
            cta_label="View requests",
            variable_help=(
                "{{ user_name }}, {{ decision_headline }}, {{ decision_body }}, {{ request_id }}, "
                "{{ equipment_name }}, {{ equipment_code }}, {{ admin_notes }}, {{ link }}"
            ),
        ),
    ]


def _wallet_templates() -> list[dict[str, Any]]:
    return [
        _wallet_email(
            code="wallet_credit_email",
            title="Wallet Credited",
            subject="Wallet Credited",
            intro="Your wallet has been credited.",
            description="Email sent when wallet is credited (refund or recharge).",
            note_vars=(("response_message", "Response"),),
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ description }}, {{ balance }}, "
                "{{ department_name }}, {{ department_code }}, {{ transaction_date }}, "
                "{{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ link }}"
            ),
        ),
        _wallet_email(
            code="wallet_debit_email",
            title="Wallet Debited",
            subject="Wallet Debited",
            intro="An amount has been debited from your wallet.",
            description="Email sent when wallet is debited (booking payment).",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ description }}, {{ balance }}, "
                "{{ department_name }}, {{ department_code }}, {{ transaction_date }}, "
                "{{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ link }}"
            ),
        ),
        _wallet_email(
            code="wallet_recharge_approved_email",
            title="Wallet Recharge Approved",
            subject="Wallet Recharge Approved",
            intro="Your wallet recharge request has been approved.",
            description="Email sent when a wallet recharge request is approved.",
            include_booking=False,
            note_vars=(
                ("project_details", "Project details"),
                ("response_message", "Response"),
            ),
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ balance }}, {{ request_id }}, "
                "{{ request_date }}, {{ project_details }}, {{ status }}, {{ response_message }}, "
                "{{ approved_by_email }}, {{ department_name }}, {{ department_code }}, {{ link }}"
            ),
        ),
        _wallet_email(
            code="wallet_recharge_rejected_email",
            title="Wallet Recharge Rejected",
            subject="Wallet Recharge Rejected",
            intro="Your wallet recharge request has been rejected.",
            description="Email sent when a wallet recharge request is rejected.",
            include_booking=False,
            note_vars=(
                ("project_details", "Project details"),
                ("response_message", "Reason"),
            ),
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, "
                "{{ project_details }}, {{ status }}, {{ response_message }}, "
                "{{ department_name }}, {{ department_code }}, {{ link }}"
            ),
        ),
        _wallet_email(
            code="wallet_recharge_pending_email",
            title="Recharge Request Submitted",
            subject="Wallet Recharge Request Submitted",
            intro=(
                "Your wallet recharge request has been submitted and is pending approval. "
                "You will be notified once a decision has been made."
            ),
            description="Email sent when a wallet recharge request is created/pending.",
            include_booking=False,
            note_vars=(("project_details", "Project details"),),
            cta_label="View request status",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, "
                "{{ project_details }}, {{ status }}, {{ department_name }}, {{ department_code }}, {{ link }}"
            ),
        ),
        _wallet_email(
            code="wallet_low_balance_email",
            title="Low Wallet Balance",
            subject="Low Wallet Balance Alert",
            intro=(
                "Your wallet balance has fallen below the threshold. "
                "Please recharge your wallet to continue making bookings."
            ),
            description="Email sent when wallet balance falls below threshold.",
            include_booking=False,
            cta_label="Recharge wallet",
            variable_help="{{ user_name }}, {{ user_email }}, {{ balance }}, {{ threshold }}, {{ link }}",
        ),
        _simple_email(
            code="wallet_recharge_request_email",
            title="Wallet Recharge Request",
            subject="Wallet Recharge Request",
            intro="A new wallet recharge request has been submitted and requires your action.",
            description="Email to accounts team when a user submits a wallet recharge request (approve/reject links).",
            name_var="user_name",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("User name", "user_name"),
                optional_detail_row("User email", "user_email"),
                optional_detail_row("Amount", "amount"),
                optional_detail_row("Department", "department_name"),
                optional_detail_row("Department code", "department_code"),
                optional_detail_row("Request date", "request_date"),
                optional_detail_row("Project name", "project_name"),
                optional_detail_row("Project code", "project_code"),
                optional_detail_row("Agency", "project_agency"),
                optional_detail_row("Project details", "project_details"),
            ],
            extra_html=(
                optional_cta_block("approve_url", label="Approve request")
                + optional_cta_block("reject_url", label="Reject request")
            ),
            cta_label="Open request",
            cta_var="link",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, "
                "{{ department_name }}, {{ department_code }}, {{ project_name }}, {{ project_code }}, "
                "{{ project_agency }}, {{ project_details }}, {{ approve_url }}, {{ reject_url }}, {{ link }}"
            ),
        ),
        _custom_branded_email(
            code="wallet_recharge_sric_office_email",
            name="Wallet Recharge – SRIC Office",
            title="Wallet Recharge Request",
            subject="Wallet Recharge Request – {{ faculty_display_name }} [{{ emp_id }}]",
            subtitle="SRIC Office notification",
            body_inner_html="\n".join(
                [
                    paragraph_html(
                        "An urgent testing-grant credit request has been submitted. "
                        "Accounts may already have been notified separately; this message is for SRIC Office awareness."
                    ),
                    details_card_html(
                        [
                            optional_detail_row("Faculty name", "faculty_display_name"),
                            optional_detail_row("Employee number", "emp_id"),
                            optional_detail_row("Email", "user_email"),
                            optional_detail_row("Amount", "amount"),
                            optional_detail_row("Department", "department_name"),
                            optional_detail_row("Department code", "department_code"),
                            optional_detail_row("Project name", "project_name"),
                            optional_detail_row("Project code", "project_code"),
                            optional_detail_row("Agency", "project_agency"),
                            optional_detail_row("Requested at", "request_date_display"),
                            optional_detail_row("Grant code for credit", "grant_code_for_credit"),
                        ],
                        heading="Request details",
                    ),
                ]
            ),
            body_text=(
                "Urgent testing-grant credit request\n\n"
                "{% if faculty_display_name %}Faculty name: {{ faculty_display_name }}\n{% endif %}"
                "{% if emp_id %}Employee number: {{ emp_id }}\n{% endif %}"
                "{% if user_email %}Email: {{ user_email }}\n{% endif %}"
                "{% if amount %}Amount: {{ amount }}\n{% endif %}"
                "{% if department_name %}Department: {{ department_name }}\n{% endif %}"
                "{% if department_code %}Department code: {{ department_code }}\n{% endif %}"
                "{% if project_name %}Project: {{ project_name }}\n{% endif %}"
                "{% if project_code %}Project code: {{ project_code }}\n{% endif %}"
                "{% if project_agency %}Agency: {{ project_agency }}\n{% endif %}"
                "{% if request_date_display %}Requested at: {{ request_date_display }}\n{% endif %}"
                "{% if grant_code_for_credit %}Grant code for credit: {{ grant_code_for_credit }}\n{% endif %}"
                "\nThe accounts team notification may already have been sent separately."
            ),
            description="SRIC Office awareness email for faculty testing-grant wallet recharge requests.",
            variable_help=(
                "{{ faculty_name }}, {{ faculty_display_name }}, {{ grant_code_for_credit }}, {{ emp_id }}, "
                "{{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, {{ request_date_display }}, "
                "{{ department_name }}, {{ department_code }}, {{ project_name }}, {{ project_code }}, "
                "{{ project_agency }}, {{ approve_url }}, {{ reject_url }}"
            ),
        ),
        _wallet_email(
            code="wallet_credit_facility_expired_email",
            title="Credit Facility Ended",
            subject="Wallet Credit Facility Ended",
            intro=(
                "The temporary credit facility linked to your pending wallet recharge has ended "
                "because the recharge was not credited via the accounts process within the allowed window. "
                "Bookings for that department sub-wallet are on hold until the recharge is realized."
            ),
            description=(
                "Sent when a recharge credit window expires without parse credit; "
                "department bookings blocked until credited."
            ),
            include_booking=False,
            cta_label="Open wallet",
            variable_help="{{ user_name }}, {{ user_email }}, {{ request_id }}, {{ department_name }}, {{ amount }}, {{ link }}",
        ),
        _wallet_email(
            code="wallet_recharge_credit_facility_activated_email",
            title="Credit Facility Active",
            subject="Wallet Credit Facility Activated",
            intro=(
                "You accepted the temporary credit facility for your pending wallet recharge. "
                "It is now active for the department sub-wallet below. Your recharge request remains "
                "pending until accounts credits it."
            ),
            description=(
                "Sent only to the faculty user when temporary wallet credit facility is activated after OTP."
            ),
            include_booking=False,
            extra_detail_rows=[
                optional_detail_row("Overdraft limit", "credit_limit_amount"),
                optional_detail_row("Credit window ends", "credit_window_end_display"),
                optional_detail_row("Credit window (days)", "credit_window_days"),
            ],
            note_vars=(("project_lines_plain", "Project details"),),
            cta_label="View request",
            variable_help=(
                "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, "
                "{{ department_name }}, {{ department_code_suffix }}, {{ project_lines_plain }}, "
                "{{ credit_limit_amount }}, {{ credit_window_end_display }}, {{ credit_window_days }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="wallet_join_request_submitted_email",
            title="Wallet Join Request",
            subject="New Wallet Join Request",
            intro="A student has requested to join your wallet. Please approve or reject from your wallet dashboard.",
            description="Email sent to faculty when a student submits a wallet join request.",
            name_var="faculty_name",
            detail_rows=[
                optional_detail_row("Request ID", "request_id"),
                optional_detail_row("Student name", "student_name"),
                optional_detail_row("Student email", "student_email"),
                optional_detail_row("Request date", "request_date"),
            ],
            note_vars=(("message", "Message"),),
            cta_label="View request",
            variable_help=(
                "{{ faculty_name }}, {{ faculty_email }}, {{ student_name }}, {{ student_email }}, "
                "{{ request_id }}, {{ request_date }}, {{ message }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="wallet_join_request_approved_email",
            title="Wallet Join Approved",
            subject="Wallet Join Request Approved",
            intro="Your request to join a faculty wallet has been approved.",
            description="Email sent to student when wallet join request is approved.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Faculty", "faculty_name"),
                optional_detail_row("Request ID", "request_id"),
            ],
            cta_label="Open wallet",
            variable_help="{{ student_name }}, {{ student_email }}, {{ faculty_name }}, {{ request_id }}, {{ link }}",
        ),
        _simple_email(
            code="wallet_join_request_rejected_email",
            title="Wallet Join Rejected",
            subject="Wallet Join Request Rejected",
            intro="Your request to join a faculty wallet has been rejected.",
            description="Email sent to student when wallet join request is rejected.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Faculty", "faculty_name"),
                optional_detail_row("Request ID", "request_id"),
            ],
            note_vars=(("message", "Reason"),),
            variable_help=(
                "{{ student_name }}, {{ student_email }}, {{ faculty_name }}, {{ request_id }}, {{ message }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="wallet_join_request_cancelled_email",
            title="Wallet Join Cancelled",
            subject="Wallet Join Request Cancelled",
            intro="A wallet join request has been cancelled.",
            description="Email sent when a wallet join request is cancelled.",
            name_var="faculty_name",
            detail_rows=[
                optional_detail_row("Student name", "student_name"),
                optional_detail_row("Request ID", "request_id"),
            ],
            variable_help="{{ faculty_name }}, {{ student_name }}, {{ request_id }}, {{ link }}",
        ),
        _simple_email(
            code="wallet_join_request_removed_email",
            title="Removed from Wallet",
            subject="Removed from Faculty Wallet",
            intro="You have been removed from a faculty wallet.",
            description="Email sent when a student is removed from a faculty wallet.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Faculty", "faculty_name"),
            ],
            variable_help="{{ student_name }}, {{ faculty_name }}, {{ link }}",
        ),
    ]


def _registration_and_support_templates() -> list[dict[str, Any]]:
    return [
        _simple_email(
            code="registration_self_verification_email",
            title="Verify Your Registration",
            subject="Verify Your Registration",
            intro=(
                f"Thank you for registering with the {PRODUCT_NAME}. "
                "Please verify your email using the button below to complete registration."
            ),
            description="Sent when user creates an account on the self-verify path.",
            name_var="name",
            cta_label="Verify my email",
            cta_var="verification_url",
            variable_help="{{ name }}, {{ verification_url }}",
        ),
        _simple_email(
            code="registration_verification_otp_email",
            title="Registration OTP",
            subject="Your Registration OTP",
            intro=(
                "Your one-time password (OTP) to confirm registration is "
                "<strong>{{ otp }}</strong>. Enter it on the registration page. "
                "This OTP expires in 10 minutes. Do not share it with anyone."
            ),
            description="Sent when registration requires OTP before admin approval.",
            name_var="name",
            cta_label="Open portal",
            cta_var="link",
            variable_help="{{ name }}, {{ otp }}, {{ link }}",
        ),
        _simple_email(
            code="registration_approval_confirmation_email",
            title="Account Approved",
            subject="Your Account Is Approved",
            intro=(
                f"Your {PRODUCT_NAME} account has been approved. "
                "You can now sign in and start booking equipment."
            ),
            description="Sent when an admin approves a user registration.",
            name_var="name",
            cta_label="Sign in",
            cta_var="link",
            variable_help="{{ name }}, {{ link }}",
        ),
        _simple_email(
            code="support_ticket_resolution_email",
            title="Support Ticket Updated",
            subject="Support Ticket {{ status_display }}",
            intro=(
                f"Thank you for contacting {PRODUCT_NAME} support. "
                "Your ticket has been marked as <strong>{{ status_display }}</strong>."
            ),
            description=(
                "Sent when a ticket is marked Resolved or Closed. "
                "Includes ticket id, subject, status, and resolution comments."
            ),
            detail_rows=[
                optional_detail_row("Ticket ID", "ticket_id"),
                optional_detail_row("Subject", "subject"),
                optional_detail_row("Status", "status_display"),
            ],
            note_vars=(("resolution_notes", "Resolution comments"),),
            cta_label="Open tickets",
            variable_help=(
                "{{ user_name }}, {{ ticket_id }}, {{ subject }}, {{ status_display }}, "
                "{{ resolution_notes }}, {{ link }}"
            ),
        ),
        _custom_branded_email(
            code="admin_bulk_email",
            name="Bulk Email (Booked Slots)",
            title="Message from the Portal",
            subject="{{ subject }}",
            body_inner_html="\n".join(
                [
                    greeting_html("user_name"),
                    paragraph_html("{{ body }}"),
                    optional_cta_block("link", label="Open portal"),
                ]
            ),
            body_text=(
                "Hello {{ user_name }},\n\n"
                "{{ body }}\n\n"
                "{% if link %}Open portal: {{ link }}{% endif %}"
            ),
            description=(
                "Default template for admin bulk email to users with booked slots. "
                "Pass {{ subject }} and {{ body }} (or edit before sending)."
            ),
            variable_help="{{ subject }}, {{ body }}, {{ user_name }}, {{ user_email }}, {{ link }}",
            preheader=f"Message from {PRODUCT_NAME}",
        ),
        _custom_branded_email(
            code="oic_monthly_report",
            name="OIC / Lab Operator Monthly Report",
            title="Equipment Utilization Report",
            subject="Equipment Utilization Report – {{ date_from }} to {{ date_to }}",
            body_inner_html="\n".join(
                [
                    paragraph_html(
                        "Please find attached the monthly equipment performance report for "
                        "<strong>{{ equipment_name }}</strong> (code: <strong>{{ equipment_codes }}</strong>) "
                        "for the period <strong>{{ date_from }}</strong> to <strong>{{ date_to }}</strong>."
                    ),
                    paragraph_html(
                        "This email is sent to Officers in charge and Lab operators associated with this equipment. "
                        "The PDF includes users served, samples, booking hours, availability and utilization, "
                        "disruption metrics, and consolidated ratings where applicable."
                    ),
                ]
            ),
            body_text=(
                "Hello,\n\n"
                "Please find attached the monthly equipment performance report for "
                "{{ equipment_name }} ({{ equipment_codes }}) for {{ date_from }} to {{ date_to }}.\n\n"
                "This email is sent to Officers in charge and Lab operators associated with this equipment."
            ),
            description=(
                "Monthly PDF report to OIC and lab operators. "
                "Schedule: equipment.send_oic_monthly_reports."
            ),
            variable_help="{{ date_from }}, {{ date_to }}, {{ equipment_codes }}, {{ equipment_name }}",
        ),
    ]


def _nomination_and_leave_templates() -> list[dict[str, Any]]:
    return [
        _simple_email(
            code="ta_operating_nomination_call_email",
            title="Call for TA Nominations",
            subject="Call for TA Nominations – {{ instrument_name }}",
            intro=(
                "Nominations are invited for Teaching Assistant (TA) operating duty for the instrument below. "
                "Please review the details and submit nominations in the portal."
            ),
            description="Call for TA operating nominations for an instrument/semester.",
            detail_rows=[
                optional_detail_row("Instrument", "instrument_name"),
                optional_detail_row("Instrument code", "instrument_code"),
                optional_detail_row("Semester", "semester_name"),
                optional_detail_row("Academic year", "academic_year_name"),
                optional_detail_row("Expected duty time", "expected_duty_time"),
            ],
            cta_label="Open nominations",
            variable_help=(
                "{{ user_name }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, "
                "{{ academic_year_name }}, {{ expected_duty_time }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="student_nomination_intimation_email",
            title="You Have Been Nominated",
            subject="Nominated for Equipment Operation – {{ instrument_name }}",
            intro=(
                "You have been nominated for equipment operation duty. "
                "Please review the details in the portal."
            ),
            description="Sent to a student when they are nominated for TA/operating duty.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Instrument", "instrument_name"),
                optional_detail_row("Instrument code", "instrument_code"),
                optional_detail_row("Semester", "semester_name"),
                optional_detail_row("Nominated by", "nominated_by_name"),
            ],
            cta_label="View nomination",
            variable_help=(
                "{{ student_name }}, {{ instrument_name }}, {{ instrument_code }}, "
                "{{ semester_name }}, {{ nominated_by_name }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="nomination_approved_student_email",
            title="Nomination Approved",
            subject="Nomination Approved – {{ instrument_name }}",
            intro="Your nomination for equipment operation has been approved.",
            description="Sent to student when their TA nomination is approved.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Instrument", "instrument_name"),
                optional_detail_row("Instrument code", "instrument_code"),
                optional_detail_row("Semester", "semester_name"),
            ],
            cta_label="Open portal",
            variable_help="{{ student_name }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, {{ link }}",
        ),
        _simple_email(
            code="nomination_rejected_student_email",
            title="Nomination Update",
            subject="Nomination Update – {{ instrument_name }}",
            intro="There is an update on your nomination for equipment operation.",
            description="Sent to student when their TA nomination is rejected.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Instrument", "instrument_name"),
                optional_detail_row("Instrument code", "instrument_code"),
                optional_detail_row("Semester", "semester_name"),
            ],
            note_vars=(("rejection_reason", "Reason"),),
            variable_help=(
                "{{ student_name }}, {{ instrument_name }}, {{ instrument_code }}, "
                "{{ semester_name }}, {{ rejection_reason }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="ta_duty_allocation_email",
            title="TA Duty Assigned",
            subject="TA Duty Assigned – {{ instrument_name }}",
            intro=(
                "You have been assigned TA (Teaching Assistant) operating duty for the booking below. "
                "Please review and accept or decline the assignment in the portal."
            ),
            description="Sent to the TA student when Admin/OIC allocates TA duty to a booking.",
            name_var="student_name",
            detail_rows=[
                optional_detail_row("Assignment ID", "assignment_id"),
                optional_detail_row("Instrument", "instrument_name"),
                optional_detail_row("Instrument code", "instrument_code"),
                optional_detail_row("Academic period", "academic_year_name"),
                optional_detail_row("Booking reference", "booking_id"),
                optional_detail_row("Booking date", "booking_date"),
                optional_detail_row("Slot start", "booking_start"),
                optional_detail_row("Slot end", "booking_end"),
                optional_detail_row("Expected duty hours", "expected_hours"),
                optional_detail_row("Allocated by", "allocated_by_name"),
            ],
            note_vars=(("allocation_notes", "Notes from allocator"),),
            cta_label="Review assignment",
            cta_var="portal_url",
            variable_help=(
                "{{ student_name }}, {{ student_email }}, {{ instrument_name }}, {{ instrument_code }}, "
                "{{ academic_year_name }}, {{ semester_name }}, {{ assignment_id }}, {{ booking_id }}, "
                "{{ booking_date }}, {{ booking_start }}, {{ booking_end }}, {{ expected_hours }}, "
                "{{ allocation_notes }}, {{ allocated_by_name }}, {{ portal_url }}"
            ),
        ),
        _simple_email(
            code="operator_leave_submitted_operator_email",
            title="Leave Request Submitted",
            subject="Leave Request Submitted",
            intro="Your leave request has been submitted successfully and is pending approval.",
            description="Sent to operator when a leave request is submitted.",
            name_var="operator_name",
            detail_rows=[
                optional_detail_row("Start date", "start_date"),
                optional_detail_row("Start session", "start_session"),
                optional_detail_row("End date", "end_date"),
                optional_detail_row("End session", "end_session"),
            ],
            note_vars=(("reason", "Reason"),),
            cta_label="Open portal",
            cta_var="link",
            variable_help=(
                "{{ app_name }}, {{ operator_name }}, {{ start_date }}, {{ start_session }}, "
                "{{ end_date }}, {{ end_session }}, {{ reason }}, {{ leave_id }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="operator_leave_submitted_oic_email",
            title="Leave Approval Needed",
            subject="Leave Approval Needed – {{ operator_name }}",
            intro="A leave request has been submitted and is pending your review.",
            description="Sent to OIC(s)/managers when an operator submits a leave request.",
            name_var="oic_name",
            detail_rows=[
                optional_detail_row("Operator", "operator_name"),
                optional_detail_row("Start date", "start_date"),
                optional_detail_row("Start session", "start_session"),
                optional_detail_row("End date", "end_date"),
                optional_detail_row("End session", "end_session"),
            ],
            note_vars=(("reason", "Reason"),),
            cta_label="Review leave",
            variable_help=(
                "{{ app_name }}, {{ oic_name }}, {{ operator_name }}, {{ start_date }}, {{ start_session }}, "
                "{{ end_date }}, {{ end_session }}, {{ reason }}, {{ leave_id }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="operator_leave_approved_operator_email",
            title="Leave Request Approved",
            subject="Leave Request Approved",
            intro="Your leave request has been approved.",
            description="Sent to operator when their leave request is approved.",
            name_var="operator_name",
            detail_rows=[
                optional_detail_row("Start date", "start_date"),
                optional_detail_row("Start session", "start_session"),
                optional_detail_row("End date", "end_date"),
                optional_detail_row("End session", "end_session"),
                optional_detail_row("Approved by", "reviewer_name"),
            ],
            variable_help=(
                "{{ app_name }}, {{ operator_name }}, {{ reviewer_name }}, {{ start_date }}, "
                "{{ start_session }}, {{ end_date }}, {{ end_session }}, {{ leave_id }}, {{ link }}"
            ),
        ),
        _simple_email(
            code="operator_leave_rejected_operator_email",
            title="Leave Request Rejected",
            subject="Leave Request Rejected",
            intro="Your leave request has been rejected.",
            description="Sent to operator when their leave request is rejected.",
            name_var="operator_name",
            detail_rows=[
                optional_detail_row("Start date", "start_date"),
                optional_detail_row("Start session", "start_session"),
                optional_detail_row("End date", "end_date"),
                optional_detail_row("End session", "end_session"),
                optional_detail_row("Rejected by", "reviewer_name"),
            ],
            note_vars=(("rejection_reason", "Reason for rejection"),),
            variable_help=(
                "{{ app_name }}, {{ operator_name }}, {{ reviewer_name }}, {{ start_date }}, "
                "{{ start_session }}, {{ end_date }}, {{ end_session }}, {{ rejection_reason }}, "
                "{{ leave_id }}, {{ link }}"
            ),
        ),
    ]


def get_default_email_templates() -> list[dict]:
    """
    Return list of dicts with keys:
    code, name, communication_type='email', subject, body_text, body_html,
    description, variable_help, is_active=True
    """
    templates: list[dict[str, Any]] = []
    templates.extend(_booking_templates())
    templates.extend(_urgent_templates())
    templates.extend(_wallet_templates())
    templates.extend(_registration_and_support_templates())
    templates.extend(_nomination_and_leave_templates())

    by_code = {t["code"]: t for t in templates}
    missing = [c for c in DEFAULT_EMAIL_TEMPLATE_CODES if c not in by_code]
    if missing:
        raise RuntimeError(f"Missing default email templates for codes: {missing}")
    extras = [c for c in by_code if c not in DEFAULT_EMAIL_TEMPLATE_CODES]
    if extras:
        raise RuntimeError(f"Unexpected default email template codes: {extras}")

    # Stable order matching DEFAULT_EMAIL_TEMPLATE_CODES
    return [by_code[code] for code in DEFAULT_EMAIL_TEMPLATE_CODES]
