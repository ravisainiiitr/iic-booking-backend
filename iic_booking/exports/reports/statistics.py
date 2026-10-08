"""Reports & Statistics: booking statistics, equipment performance and the faculty wallet expense report."""

from __future__ import annotations

from .. import spec
from ..bridge import call_view
from ..http import ExportError
from ..registry import register
from .booking_activity import humanize
from .common import filter_pairs
from .common import make_document

C = spec.Column

_SCOPES = {
    "personal": "Your bookings",
    "wallet_group": "Your bookings and your linked students' bookings",
    "equipment": "Bookings on equipment you are assigned to",
    "department": "Bookings on your department's equipment",
    "institute": "All bookings (institute)",
}
_RATING_LABELS = {
    "on_time_operator_availability": "On-time operator availability",
    "laboratory_cleanliness_organization": "Laboratory cleanliness and organisation",
    "sample_handling_care": "Sample handling care",
    "operator_behaviour_professionalism": "Operator behaviour and professionalism",
    "compliance_booking_request_parameters": "Compliance with booking request parameters",
}


def _status_labels() -> dict:
    from iic_booking.equipment.booking_list_status import DERIVED_LABELS
    from iic_booking.equipment.models import BookingStatus

    return {**{str(k): str(v) for k, v in BookingStatus.choices}, **DERIVED_LABELS}


# ---------------------------------------------------------------------------
# Booking statistics (cards + status breakdown at the top of Reports & Statistics)
# ---------------------------------------------------------------------------


def _money_visible(data: dict) -> bool:
    """The source endpoints omit money for viewers who may not see it (Lab Operators)."""
    return data.get("revenue_visible") is not False


def _booking_stats_parts(request, *, params=None):
    data = call_view(request, "booking-stats", params if params is not None else {
        k: request.query_params.get(k) for k in ("status", "date_from", "date_to") if request.query_params.get(k)
    }) or {}
    labels = _status_labels()
    staff_scope = data.get("scope") in ("equipment", "department", "institute")
    kpis = [spec.Kpi("Total bookings", data.get("total_bookings", 0), spec.INTEGER, _SCOPES.get(data.get("scope"), ""))]
    if _money_visible(data):
        kpis.append(spec.Kpi("Total charged" if staff_scope else "Total spent", data.get("total_spent", 0),
                             spec.CURRENCY, f"{int(data.get('charged_bookings') or 0):,} charged bookings"))
    kpis.append(spec.Kpi("Total hours booked", data.get("total_hours", 0), spec.NUMBER))
    if _money_visible(data):
        kpis += [
            spec.Kpi("Average cost per booking", data.get("average_cost", 0), spec.CURRENCY),
            spec.Kpi("Refunded amount", data.get("refunded_amount", 0), spec.CURRENCY),
        ]
    counts = data.get("status_counts") or {}
    total = sum(int(v or 0) for v in counts.values()) or 0
    rows = [
        {"status": labels.get(k, humanize(k)), "count": int(v or 0), "share": (int(v or 0) / total) if total else 0}
        for k, v in counts.items()
    ]
    table = spec.Table(
        "booking_status",
        "Booking status breakdown",
        [C("status", "Status", width=2.0), C("count", "Bookings", spec.INTEGER, 1.0, total=True),
         C("share", "Share", spec.PERCENT, 1.0)],
        rows,
        sheet_name="Booking status",
        empty_message="No bookings in your scope.",
    )
    return data, kpis, table


@register("booking-statistics")
def booking_statistics(request):
    data, kpis, table = _booking_stats_parts(request)
    labels = _status_labels()
    filters = [("Scope", _SCOPES.get(data.get("scope"), humanize(data.get("scope"))))]
    filters += filter_pairs(request, [("status", "Status", labels), ("date_from", "Created from", "date"),
                                      ("date_to", "Created to", "date")])
    return make_document(request, title="Booking Statistics", slug="booking-statistics", tables=[table],
                         filters=filters, kpis=kpis, subtitle=_SCOPES.get(data.get("scope"), ""))


@register("report-bookings")
def report_bookings(request):
    """Reports › Booking details: every booking in the report scope with hours and amount (hours only for operators)."""
    from ..bridge import collect_rows

    status = (request.query_params.get("status") or "").strip()
    stats, kpis, _ = _booking_stats_parts(request, params={"status": status} if status else {})
    money = _money_visible(stats)
    params = {"list_view": "true", "ordering": "-created_at"}
    if status:
        params["list_status"] = status
    rows, _ = collect_rows(request, "list-bookings", results_key="bookings", page_size=100, params=params)
    labels = _status_labels()
    columns = [
        C("booking_id", "Booking ID", width=1.1),
        C("equipment", "Equipment", width=1.9, value=lambda r: _eq_label(
            {"code": r.get("equipment_code"), "name": r.get("equipment_name")})),
        C("start_time", "Start (IST)", spec.DATETIME, 1.15),
        C("end_time", "End (IST)", spec.DATETIME, 1.15),
        C("total_hours", "Hours", spec.NUMBER, 0.6, total=True),
        *([C("total_charge", "Amount (₹)", spec.CURRENCY, 0.9, total=True)] if money else []),
        C("status", "Status", width=0.9,
          value=lambda r: r.get("status_display") or labels.get(r.get("status"), humanize(r.get("status")))),
        C("rating", "Rating", spec.NUMBER, 0.5),
        C("created_at", "Booked on (IST)", spec.DATETIME, 1.15),
    ]
    filters = [("Scope", _SCOPES.get(stats.get("scope"), humanize(stats.get("scope"))))]
    filters += filter_pairs(request, [("status", "Status", labels)])
    table = spec.Table("bookings", "Booking details", columns, rows, empty_message="No bookings in your scope.")
    kpis = [k for k in kpis if k.label != "Refunded amount"]
    title = "Booking Details — Amount and Hours" if money else "Booking Details — Hours"
    return make_document(request, title=title, slug="booking-details", tables=[table], filters=filters, kpis=kpis,
                         subtitle=_SCOPES.get(stats.get("scope"), ""))


# ---------------------------------------------------------------------------
# Equipment performance report (admin panel / reports staff)
# ---------------------------------------------------------------------------


def _names(people) -> str:
    return ", ".join(p.get("name") or p.get("email") or "" for p in people or [] if p)


def _eq_label(row) -> str:
    code, name = row.get("code") or "", row.get("name") or ""
    return f"{name} ({code})" if code and name else (name or code)


def _revenue_table(key, title, label_header, label_value, rows, sheet):
    return spec.Table(
        key, title,
        [C("label", label_header, width=2.4, value=label_value),
         C("count", "Bookings", spec.INTEGER, 0.8, total=True),
         C("total", "Revenue (₹)", spec.CURRENCY, 1.0, total=True)],
        list(rows or []), sheet_name=sheet, empty_message="No completed bookings in this period.",
    )


def _equipment_parts(request):
    params = request.query_params.copy()
    for key in list(params.keys()):
        if key not in ("date_from", "date_to", "equipment_id"):
            params.pop(key)
    data = call_view(request, "admin-equipment-reports-list", params) or {}
    summary = data.get("summary") or {}
    financial = data.get("financial") or {}
    equipment = data.get("equipment") or []

    money = _money_visible(data)
    kpis = [spec.Kpi("Equipment", summary.get("total_equipment", len(equipment)), spec.INTEGER)]
    if money:
        kpis += [
            spec.Kpi("Revenue (total)", summary.get("revenue_total", 0), spec.CURRENCY, "Completed bookings in period"),
            spec.Kpi("Revenue (internal)", summary.get("revenue_internal", 0), spec.CURRENCY),
            spec.Kpi("Revenue (external)", summary.get("revenue_external", 0), spec.CURRENCY),
        ]
    kpis += [
        spec.Kpi("Utilization factor", summary.get("utilization_factor", 0), spec.PERCENT,
                 "Booked hours ÷ all slot hours"),
        spec.Kpi("Utilized hours", summary.get("utilized_hours", 0), spec.NUMBER),
        spec.Kpi("Downtime hours", summary.get("downtime_hours", 0), spec.NUMBER,
                 "Under / scheduled maintenance + operator absent"),
        spec.Kpi("Disruption hours", summary.get("disruption_hours", 0), spec.NUMBER,
                 "Downtime + Other Reasons recorded as disruptions"),
        spec.Kpi("Available hours (work window)", summary.get("available_hours_working_window", 0), spec.NUMBER),
        spec.Kpi("Completed hours (work window)", summary.get("completed_hours_in_working_window", 0), spec.NUMBER),
        spec.Kpi("Utilization vs working capacity", summary.get("utilization_vs_working_capacity", 0),
                 spec.PERCENT),
    ]

    tables = [] if not money else [
        _revenue_table("revenue_user_type", "Revenue by user type", "User type",
                       lambda r: humanize(r.get("user_type_snapshot")) or "—",
                       financial.get("revenue_by_user_type"), "Revenue by user type"),
        _revenue_table("revenue_department", "Revenue by department", "Department",
                       lambda r: r.get("user__department__name") or "—",
                       financial.get("revenue_by_department"), "Revenue by department"),
        _revenue_table("revenue_equipment", "Revenue by equipment", "Equipment",
                       lambda r: _eq_label({"code": r.get("equipment__code"), "name": r.get("equipment__name")}),
                       financial.get("revenue_by_equipment"), "Revenue by equipment"),
        _revenue_table("revenue_external", "External revenue by category", "Category",
                       lambda r: humanize(r.get("user_type_snapshot")) or "—",
                       financial.get("revenue_by_external_category"), "External revenue"),
    ]
    pie = [p for p in data.get("utilization_pie") or [] if float(p.get("hours") or 0) > 0]
    pie_total = sum(float(p.get("hours") or 0) for p in pie)
    tables.append(spec.Table(
        "utilization", "Overall equipment utilization",
        [C("name", "Slot outcome", width=2.2), C("hours", "Hours", spec.NUMBER, 1.0, total=True),
         C("share", "Share", spec.PERCENT, 1.0, value=lambda r: (float(r.get("hours") or 0) / pie_total)
           if pie_total else 0)],
        pie, sheet_name="Utilization", empty_message="No slot data in this period.",
    ))
    tables.append(spec.Table(
        "equipment_usage", "Equipment-wise usage",
        [
            C("equipment", "Equipment", width=2.0, value=_eq_label),
            C("status_display", "Status", width=0.8),
            C("oic", "Officer(s) in charge", width=1.6, value=lambda r: _names(r.get("officers_in_charge"))),
            C("ops", "Lab operator(s)", width=1.6, value=lambda r: _names(r.get("lab_operators"))),
            C("distinct_users_internal", "Users (internal)", spec.INTEGER, 0.7, total=True),
            C("distinct_users_external", "Users (external)", spec.INTEGER, 0.7, total=True),
            C("distinct_users_served", "Users (total)", spec.INTEGER, 0.7, total=True),
            C("samples_internal", "Samples (internal)", spec.INTEGER, 0.7, total=True),
            C("samples_external", "Samples (external)", spec.INTEGER, 0.7, total=True),
            C("total_samples", "Samples (total)", spec.INTEGER, 0.7, total=True),
            C("booking_hours_internal", "Booking hours (internal)", spec.NUMBER, 0.8, total=True),
            C("booking_hours_external", "Booking hours (external)", spec.NUMBER, 0.8, total=True),
            C("total_booking_hours", "Booking hours (total)", spec.NUMBER, 0.8, total=True),
        ],
        equipment, sheet_name="Equipment usage", empty_message="No equipment in this report.",
    ))
    tables.append(spec.Table(
        "equipment_capacity", "Equipment-wise availability and utilization",
        [
            C("equipment", "Equipment", width=2.0, value=_eq_label),
            C("slot_window_display", "Slot window", width=1.1),
            C("available_hours_working_window", "Available h (work window)", spec.NUMBER, 0.9, total=True),
            C("completed_slot_hours_working_window", "Completed h (work window)", spec.NUMBER, 0.9, total=True),
            C("utilization_vs_working_capacity", "Utilization vs capacity", spec.PERCENT, 0.9),
            C("available_hours_weekend_or_holiday", "Available h (weekend/holiday)", spec.NUMBER, 0.9, total=True),
            C("blocked_hours", "Blocked h", spec.NUMBER, 0.7, total=True),
            C("other_disruption_hours", "Other disruption h", spec.NUMBER, 0.8, total=True),
            C("total_bookings_in_period", "Bookings in period", spec.INTEGER, 0.8, total=True),
            C("completed_in_period", "Completed in period", spec.INTEGER, 0.8, total=True),
            C("overall_current_bookings", "Current bookings", spec.INTEGER, 0.8, total=True),
            C("overall_bookings", "Bookings (all time)", spec.INTEGER, 0.8, total=True),
        ],
        equipment, sheet_name="Availability", empty_message="No equipment in this report.",
    ))
    tables.append(spec.Table(
        "slot_outcomes", "Equipment-wise slot outcomes",
        [
            C("equipment", "Equipment", width=2.0, value=_eq_label),
            C("booked_slots", "Booked slots", spec.INTEGER, 0.7, total=True),
            C("booked_hours", "Booked h", spec.NUMBER, 0.7, total=True),
            C("booking_not_utilized_slots", "Not utilized slots", spec.INTEGER, 0.7, total=True),
            C("booking_not_utilized_hours", "Not utilized h", spec.NUMBER, 0.7, total=True),
            C("under_maintenance_slots", "Maintenance slots", spec.INTEGER, 0.7, total=True),
            C("under_maintenance_hours", "Maintenance h", spec.NUMBER, 0.7, total=True),
            C("operator_absent_slots", "Operator absent slots", spec.INTEGER, 0.7, total=True),
            C("operator_absent_hours", "Operator absent h", spec.NUMBER, 0.7, total=True),
            C("scheduled_maintenance_hours", "Scheduled maint. h", spec.NUMBER, 0.7, total=True),
            C("other_reasons_hours", "Other reasons h", spec.NUMBER, 0.7, total=True),
            C("disruption_hours", "Disruption h", spec.NUMBER, 0.7, total=True),
            C("no_booking_slots", "No booking slots", spec.INTEGER, 0.7, total=True),
            C("no_booking_hours", "No booking h", spec.NUMBER, 0.7, total=True),
        ],
        equipment, sheet_name="Slot outcomes", empty_message="No equipment in this report.",
    ))
    rating_columns = [
        C("equipment", "Equipment", width=2.0, value=_eq_label),
        C("user_ratings.ratings_submitted_count", "Ratings", spec.INTEGER, 0.6, total=True),
        C("user_ratings.overall_rating_avg", "Average", spec.NUMBER, 0.6),
        C("user_ratings.overall_rating_min", "Min", spec.NUMBER, 0.5),
        C("user_ratings.overall_rating_max", "Max", spec.NUMBER, 0.5),
    ]
    for key, label in _RATING_LABELS.items():
        rating_columns.append(C(f"user_ratings.criteria.{key}", f"{label} (yes / no)", width=1.0,
                                value=lambda r, k=key: _yes_no((r.get("user_ratings") or {}).get("criteria", {}).get(k))))
    tables.append(spec.Table("ratings", "User ratings (rate your experience)", rating_columns, equipment,
                             sheet_name="Ratings", empty_message="No equipment in this report."))
    feedback = [
        {"equipment": _eq_label(eq), "feedback": text}
        for eq in equipment for text in (eq.get("user_ratings") or {}).get("sample_feedback_texts") or []
    ]
    if feedback:
        tables.append(spec.Table("feedback", "Rating feedback (latest, up to 50 per equipment)",
                                 [C("equipment", "Equipment", width=1.4), C("feedback", "Feedback", width=4.0)],
                                 feedback, sheet_name="Feedback"))
    header = data.get("report_header") or {}
    period = (header.get("period_display") or f"{data.get('date_from', '')} – {data.get('date_to', '')}")
    period += header.get("report_duration_suffix") or ""
    return data, kpis, tables, period, header


def _yes_no(criteria) -> str:
    if not criteria:
        return ""
    return f"{int(criteria.get('yes') or 0)} / {int(criteria.get('no') or 0)}"


def _equipment_filters(request, period: str) -> list:
    filters = [("Report period", period)]
    filters += filter_pairs(request, [("equipment_id", "Equipment", "equipment")])
    if not request.query_params.get("equipment_id"):
        filters.append(("Equipment", "All equipment you can report on"))
    return filters


@register("equipment-performance")
def equipment_performance(request):
    data, kpis, tables, period, header = _equipment_parts(request)
    doc = make_document(request, title=header.get("report_title") or "Equipment Performance Report",
                        slug="equipment-performance-report", tables=tables,
                        filters=_equipment_filters(request, period), kpis=kpis, subtitle=period)
    if header.get("department_name"):
        doc.department = header["department_name"]
    return doc


@register("reports-statistics")
def reports_statistics(request):
    """The whole Reports & Statistics page: booking statistics plus, for report staff, equipment performance."""
    stats, stats_kpis, status_table = _booking_stats_parts(request, params={})
    status_table.note = "All bookings in your scope (not limited to the report period), as on the Reports page."
    kpis = list(stats_kpis)
    tables = [status_table]
    filters = [("Booking statistics scope", _SCOPES.get(stats.get("scope"), humanize(stats.get("scope"))))]
    subtitle = _SCOPES.get(stats.get("scope"), "")
    try:
        _, eq_kpis, eq_tables, period, _ = _equipment_parts(request)
    except ExportError as exc:
        if exc.status != 403:
            raise
    else:
        kpis += eq_kpis
        tables += eq_tables
        filters += _equipment_filters(request, period)
        subtitle = period
    return make_document(request, title="Reports & Statistics", slug="reports-and-statistics", tables=tables,
                         filters=filters, kpis=kpis, subtitle=subtitle)


# ---------------------------------------------------------------------------
# Faculty wallet expense report (faculty view of Reports & Statistics)
# ---------------------------------------------------------------------------


@register("faculty-expense-report")
def faculty_expense_report(request):
    data = call_view(request, "wallet-faculty-expense-report") or {}
    movements = data.get("period_wallet_movements") or {}
    spend = data.get("period_booking_spend") or {}
    kpis = [
        spec.Kpi("Current balance", data.get("current_balance"), spec.CURRENCY),
        spec.Kpi("Booking spend in period", spend.get("total"), spec.CURRENCY,
                 f"{int(spend.get('booking_count') or 0):,} bookings"),
        spec.Kpi("Total debits", movements.get("total_debits"), spec.CURRENCY),
        spec.Kpi("Total credits", movements.get("total_credits"), spec.CURRENCY),
        spec.Kpi("Recharges", movements.get("recharges_and_similar_credits"), spec.CURRENCY),
        spec.Kpi("Refund credits", movements.get("refund_credits"), spec.CURRENCY),
        spec.Kpi("Internal transfer credits", movements.get("internal_transfer_credits"), spec.CURRENCY),
        spec.Kpi("Withdrawal reversals", movements.get("withdrawal_reversal_credits"), spec.CURRENCY),
    ]
    members = data.get("by_member") or []
    member_equipment = [
        {"member": m.get("name") or m.get("email"), "role": m.get("role_label"), **eq}
        for m in members for eq in m.get("by_equipment") or []
    ]
    tables = [
        spec.Table("by_member", "Spend by member", [
            C("name", "Member", width=1.5),
            C("email", "Email", width=1.7),
            C("role_label", "Role", width=0.9),
            C("booking_count", "Bookings", spec.INTEGER, 0.7, total=True),
            C("total_spend", "Spend (₹)", spec.CURRENCY, 0.9, total=True),
            C("share_of_period_spend_percent", "Share", spec.PERCENT, 0.7,
              value=lambda r: (float(r.get("share_of_period_spend_percent") or 0) / 100)),
        ], members, sheet_name="By member", empty_message="No spend in this period."),
        spec.Table("by_equipment", "Spend by equipment", [
            C("equipment", "Equipment", width=2.4, value=lambda r: _eq_label(
                {"code": r.get("equipment_code"), "name": r.get("equipment_name")})),
            C("booking_count", "Bookings", spec.INTEGER, 0.8, total=True),
            C("total_spend", "Spend (₹)", spec.CURRENCY, 1.0, total=True),
        ], data.get("by_equipment") or [], sheet_name="By equipment", empty_message="No spend in this period."),
        spec.Table("member_equipment", "Spend by member and equipment", [
            C("member", "Member", width=1.5),
            C("role", "Role", width=0.9),
            C("equipment", "Equipment", width=2.0, value=lambda r: _eq_label(
                {"code": r.get("equipment_code"), "name": r.get("equipment_name")})),
            C("booking_count", "Bookings", spec.INTEGER, 0.7, total=True),
            C("total_spend", "Spend (₹)", spec.CURRENCY, 0.9, total=True),
        ], member_equipment, sheet_name="Member x equipment", empty_message="No spend in this period."),
        spec.Table("sub_wallets", "Department sub-wallets", [
            C("department_name", "Department", width=2.4),
            C("balance", "Balance (₹)", spec.CURRENCY, 1.0, total=True),
        ], data.get("sub_wallets") or [], sheet_name="Sub-wallets", empty_message="No department sub-wallets."),
    ]
    filters = [("Period", f"{_dmy(data.get('date_from'))} – {_dmy(data.get('date_to'))}")]
    filters += filter_pairs(request, [("equipment_id", "Equipment", "equipment")])
    return make_document(request, title="Wallet Expense Report", slug="wallet-expense-report", tables=tables,
                         filters=filters, kpis=kpis, subtitle="Your wallet and linked students")


def _dmy(value) -> str:
    from ..values import display_text

    return display_text(value, spec.DATE) if value else "—"
