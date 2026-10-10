"""Booking activity lists: attempt logs, urgent requests, waitlist and repeat sample requests."""

from __future__ import annotations

from .. import spec
from ..bridge import collect_rows
from ..registry import register
from .common import SNO
from .common import booking_link
from .common import filter_pairs
from .common import make_document
from .common import numbered

C = spec.Column


def humanize(value) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").capitalize() if text else ""


def _choices(model_choices) -> dict:
    return {str(k): str(v) for k, v in model_choices.choices}


def _equipment(row) -> str:
    code, name = row.get("equipment_code") or "", row.get("equipment_name") or ""
    return f"{name} ({code})" if code and name else (name or code)


def _duration(row, key="duration_minutes") -> str:
    minutes = row.get(key)
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    return f"{hours} h {mins} min" if hours and mins else (f"{hours} h" if hours else f"{mins} min")


def _attempt_reason(row) -> str:
    if row.get("outcome") == "SUCCESS":
        return ""
    title = (row.get("failure_title") or "").strip()
    summary = (row.get("failure_summary") or "").strip()
    if title and summary:
        return f"{title}: {summary}"
    return title or summary or (row.get("failure_reason") or "")


_OUTCOMES = {"SUCCESS": "Successful", "FAILED": "Failed"}
_DATE_FILTERS = [("date_from", "From", "date"), ("date_to", "To", "date")]


@register("booking-attempt-logs")
def booking_attempt_logs(request):
    rows, _ = collect_rows(request, "list-booking-attempt-logs", page_size=200)
    columns = [
        SNO,
        C("requested_at", "Attempted at (IST)", spec.DATETIME, 1.2),
        C("user_name", "User", width=1.3),
        C("user_email", "Email", width=1.6),
        C("equipment", "Equipment", width=1.6, value=_equipment),
        C("outcome", "Outcome", width=0.8, value=lambda r: _OUTCOMES.get(r.get("outcome"), humanize(r.get("outcome")))),
        C("reason", "Reason (if failed)", width=2.6, value=_attempt_reason, align="left"),
        C("display_booking_id", "Booking ID", width=1.1, link=booking_link(request, display_key="display_booking_id")),
        C("number_of_samples", "Samples", spec.INTEGER, 0.6),
        C("slots_requested", "Slots", spec.INTEGER, 0.5),
        C("duration", "Duration", width=0.7, value=_duration),
    ]
    filters = filter_pairs(request, [
        ("equipment_id", "Equipment", "equipment"),
        ("user_id", "User", "user"),
        ("department_id", "Department", "department"),
        ("outcome", "Outcome", _OUTCOMES),
        *_DATE_FILTERS,
        ("failure_reason_contains", "Reason contains", "text"),
    ])
    table = spec.Table("attempts", "Booking attempt log", columns, numbered(rows),
                       empty_message="No booking attempts match these filters.")
    return make_document(request, title="Booking Attempt Log", slug="booking-attempt-log", tables=[table],
                         filters=filters, landscape=True)


def _slots_text(row) -> str:
    from ..values import display_text

    parts = []
    for slot in row.get("requested_slots") or []:
        start = display_text(slot.get("start_datetime"), spec.DATETIME)
        end = display_text(slot.get("end_datetime"), spec.DATETIME)
        if start and end:
            end_time = end.split(", ")[-1] if start.split(", ")[0] == end.split(", ")[0] else end
            parts.append(f"{start} – {end_time}")
    return "\n".join(parts)


def _booked_note(row) -> str:
    if row.get("booked_by_name"):
        return f"Booked for you by {row['booked_by_name']}"
    if row.get("booked_for_name"):
        return f"You booked for {row['booked_for_name']}"
    return ""


@register("my-booking-attempts")
def my_booking_attempts(request):
    rows, _ = collect_rows(request, "my-booking-attempts", page_size=100)
    columns = [
        SNO,
        C("requested_at", "Attempted at (IST)", spec.DATETIME, 1.2),
        C("equipment", "Equipment", width=1.7, value=_equipment),
        C("outcome", "Outcome", width=0.8, value=lambda r: _OUTCOMES.get(r.get("outcome"), humanize(r.get("outcome")))),
        C("slots", "Requested slots", width=2.0, value=_slots_text),
        C("reason", "Reason", width=2.6, value=_attempt_reason, align="left"),
        C("booked", "Booked by / for", width=1.3, value=_booked_note),
    ]
    outcome = (request.query_params.get("outcome") or "FAILED").strip().upper()
    filters = [("Outcome", {"FAILED": "Unsuccessful", "SUCCESS": "Successful"}.get(outcome, "All"))]
    filters += filter_pairs(request, _DATE_FILTERS)
    table = spec.Table("attempts", "My booking attempts", columns, numbered(rows),
                       empty_message="No booking attempts match these filters.")
    return make_document(request, title="My Booking Attempts", slug="my-booking-attempts", tables=[table],
                         filters=filters)


def _urgent_labels():
    from iic_booking.equipment.models import UrgentBookingRequestStatus
    from iic_booking.equipment.models import UrgentBookingRequestType

    return _choices(UrgentBookingRequestStatus), _choices(UrgentBookingRequestType)


def _urgent_type(types):
    def value(row):
        label = types.get(row.get("request_type"), humanize(row.get("request_type")))
        if row.get("waive_urgent_surcharge"):
            label += " (surcharge waived)"
        return label
    return value


def _supervisor_text(row) -> str:
    decision = (row.get("supervisor_decision") or "").strip()
    if not row.get("supervisor_approval_required") and not decision:
        return "Not required"
    who = row.get("supervisor_name") or ""
    state = humanize(decision) if decision else "Pending"
    return f"{state} — {who}" if who else state


def _hold_booking_link(request):
    link = booking_link(request)
    return lambda row: link(row.get("hold_booking_summary") or {})


@register("urgent-requests")
def urgent_requests(request):
    statuses, types = _urgent_labels()
    rows, _ = collect_rows(request, "list-urgent-booking-requests", results_key="urgent_requests", page_size=100)
    columns = [
        SNO,
        C("requested_at", "Requested at (IST)", spec.DATETIME, 1.15),
        C("user_name", "User", width=1.25),
        C("user_email", "Email", width=1.5),
        C("equipment", "Equipment", width=1.5, value=_equipment),
        C("request_type", "Type", width=1.5, value=_urgent_type(types)),
        C("status", "Status", width=0.8, value=lambda r: statuses.get(r.get("status"), humanize(r.get("status")))),
        C("number_of_samples", "Samples", spec.INTEGER, 0.6),
        C("slots_requested", "Slots", spec.INTEGER, 0.5),
        C("supervisor", "Supervisor approval", width=1.3, value=_supervisor_text),
        C("hold_booking_summary.booking_id", "Hold booking", width=1.0, link=_hold_booking_link(request)),
        C("expiry_at", "Hold expires (IST)", spec.DATETIME, 1.15),
        C("decided_by_name", "Decided by", width=1.1),
        C("decided_at", "Decided at (IST)", spec.DATETIME, 1.15),
        C("admin_notes", "Staff notes", width=1.6, align="left"),
        C("no_slot_log_count", "No-slot attempts", spec.INTEGER, 0.7),
    ]
    filters = filter_pairs(request, [
        ("status", "Status", statuses),
        ("request_type", "Type", types),
        ("department_id", "Department", "department"),
        ("equipment_id", "Equipment", "equipment"),
    ])
    table = spec.Table("urgent", "Urgent booking requests", columns, numbered(rows),
                       empty_message="No urgent booking requests match these filters.")
    return make_document(request, title="Urgent Booking Requests", slug="urgent-booking-requests", tables=[table],
                         filters=filters, landscape=True)


@register("my-urgent-requests")
def my_urgent_requests(request):
    statuses, types = _urgent_labels()
    rows, _ = collect_rows(request, "list-my-urgent-booking-requests", results_key="urgent_requests", page_size=100)
    columns = [
        SNO,
        C("requested_at", "Requested at (IST)", spec.DATETIME, 1.2),
        C("equipment", "Equipment", width=1.8, value=_equipment),
        C("request_type", "Type", width=1.7, value=_urgent_type(types)),
        C("status", "Status", width=0.9, value=lambda r: statuses.get(r.get("status"), humanize(r.get("status")))),
        C("supervisor_decision", "Supervisor decision", width=1.1,
          value=lambda r: humanize(r.get("supervisor_decision")) or ("Pending" if r.get("pending_wallet_approval") else "")),
        C("decided_at", "Decided at (IST)", spec.DATETIME, 1.2),
        C("expiry_at", "Hold expires (IST)", spec.DATETIME, 1.2),
    ]
    filters = filter_pairs(request, [("status", "Status", statuses)])
    table = spec.Table("urgent", "My urgent booking requests", columns, numbered(rows),
                       empty_message="You have no urgent booking requests matching these filters.")
    return make_document(request, title="My Urgent Booking Requests", slug="my-urgent-requests", tables=[table],
                         filters=filters)


@register("urgent-requests-wallet")
def urgent_requests_wallet(request):
    statuses, types = _urgent_labels()
    rows, _ = collect_rows(request, "list-urgent-requests-wallet", results_key="urgent_requests", page_size=100)
    columns = [
        SNO,
        C("requested_at", "Requested at (IST)", spec.DATETIME, 1.15),
        C("user_name", "Student", width=1.3),
        C("user_email", "Email", width=1.5),
        C("equipment", "Equipment", width=1.5, value=_equipment),
        C("request_type", "Type", width=1.5, value=_urgent_type(types)),
        C("wallet_status", "Your decision", width=0.9, value=lambda r: humanize(r.get("wallet_status"))),
        C("status", "Request status", width=0.9, value=lambda r: statuses.get(r.get("status"), humanize(r.get("status")))),
        C("number_of_samples", "Samples", spec.INTEGER, 0.6),
        C("hold_booking_total_charge", "Hold booking charge", spec.CURRENCY, 0.9),
        C("wallet_approved_at", "Decided at (IST)", spec.DATETIME, 1.15),
        C("wallet_notes", "Notes", width=1.5, align="left"),
    ]
    filters = filter_pairs(request, [("status", "Status", {"PENDING": "Pending", "APPROVED": "Approved",
                                                            "REJECTED": "Rejected", "ALL": "All"})])
    table = spec.Table("urgent", "Urgent requests on my wallet", columns, numbered(rows),
                       empty_message="No urgent requests match these filters.")
    return make_document(request, title="Urgent Requests — Wallet Approvals", slug="urgent-requests-wallet",
                         tables=[table], filters=filters, landscape=True)


def _waitlist_status(row) -> str:
    if row.get("opted_out"):
        return "Opted out"
    status = (row.get("status") or "").strip()
    if status.upper() == "CANNOT_FULFILL":
        remark = (row.get("cannot_fulfill_remark") or "").strip()
        return f"Cannot fulfil — {remark}" if remark else "Cannot fulfil"
    if row.get("awaiting_confirmation"):
        return "Awaiting confirmation"
    return humanize(status) or "Active"


def _sample_text(row) -> str:
    if not row.get("sample_submitted"):
        return "Not submitted"
    bits = [row.get("sample_identifiers") or "", row.get("sample_tracking_id") or ""]
    return "Submitted" + (": " + " · ".join(b for b in bits if b) if any(bits) else "")


def _last_attempt(row) -> str:
    title = (row.get("booking_attempt_failure_title") or "").strip()
    summary = (row.get("booking_attempt_failure_summary") or row.get("booking_attempt_failure_reason") or "").strip()
    return f"{title}: {summary}" if title and summary else (title or summary)


@register("waitlist")
def waitlist(request):
    rows, data = collect_rows(request, "admin-equipment-waitlist-all", results_key="entries", page_size=None)
    columns = [
        SNO,
        C("equipment", "Equipment", width=1.6, value=_equipment),
        C("position", "Position", spec.INTEGER, 0.75),
        C("waitlist_code", "Waitlist ID", width=1.0),
        C("user_name", "User", width=1.3),
        C("user_email", "Email", width=1.6),
        C("created_at", "Joined (IST)", spec.DATETIME, 1.15),
        C("status", "Status", width=1.4, value=_waitlist_status),
        C("sample", "Sample", width=1.3, value=_sample_text),
        C("booking_attempt_requested_at", "Last attempt (IST)", spec.DATETIME, 1.15),
        C("last_attempt", "Last attempt result", width=2.0, value=_last_attempt, align="left"),
    ]
    kpis = []
    if isinstance(data, dict):
        kpis = [
            spec.Kpi("Entries", data.get("count", len(rows)), spec.INTEGER),
            spec.Kpi("Active", data.get("active_count"), spec.INTEGER),
            spec.Kpi("Cannot fulfil", data.get("cannot_fulfill_count"), spec.INTEGER),
            spec.Kpi("Opted out", data.get("opted_out_count"), spec.INTEGER),
        ]
    filters = filter_pairs(request, [("department_id", "Department", "department"),
                                     ("equipment_id", "Equipment", "equipment")])
    table = spec.Table("waitlist", "Waitlisted bookings", columns, numbered(rows),
                       empty_message="No one is on the waitlist for these filters.")
    return make_document(request, title="Equipment Waitlist", slug="equipment-waitlist", tables=[table],
                         filters=filters, kpis=kpis, landscape=True)


_REPEAT_STATUSES = {"PENDING": "Pending", "APPROVED": "Approved", "REJECTED": "Rejected"}


def _repeat_status(row) -> str:
    return row.get("status_display") or _REPEAT_STATUSES.get(row.get("status"), humanize(row.get("status")))


@register("repeat-sample-requests")
def repeat_sample_requests(request):
    rows, _ = collect_rows(request, "list-repeat-sample-requests", results_key="repeat_sample_requests",
                           page_size=None)
    columns = [
        SNO,
        C("booking_id", "Original booking", width=1.1, link=booking_link(request)),
        C("equipment", "Equipment", width=1.6, value=_equipment),
        C("user_name", "User", width=1.3),
        C("user_email", "Email", width=1.6),
        C("completed_at", "Original completed (IST)", spec.DATETIME, 1.15),
        C("requested_at", "Marked (IST)", spec.DATETIME, 1.15),
        C("status", "Status", width=0.8, value=_repeat_status),
        C("responded_by_name", "Arranged by", width=1.2),
        C("new_booking_id", "Repeat booking", width=1.1,
          link=booking_link(request, display_key="new_booking_id", pk_key="new_real_booking_id")),
        C("booked_at", "Repeat booked (IST)", spec.DATETIME, 1.15),
        C("notes", "Notes", width=1.8, value=lambda r: r.get("admin_notes") or r.get("user_notes") or "",
          align="left"),
    ]
    filters = filter_pairs(request, [("status", "Status", _REPEAT_STATUSES),
                                     ("department_id", "Department", "department"),
                                     ("equipment_id", "Equipment", "equipment")])
    table = spec.Table("repeats", "Repeat samples", columns, numbered(rows),
                       empty_message="No repeat samples match these filters.")
    return make_document(request, title="Repeat Samples", slug="repeat-samples", tables=[table],
                         filters=filters, landscape=True)
