"""Disruption history (Under Maintenance, Operator Absent, Scheduled Maintenance, Other Reasons)."""

from __future__ import annotations

from .. import spec
from ..bridge import call_view
from ..bridge import collect_rows
from ..registry import register
from .common import SNO
from .common import filter_pairs
from .common import make_document
from .common import numbered

C = spec.Column

TYPE_LABELS = {
    "UNDER_MAINTENANCE": "Under Maintenance",
    "OPERATOR_ABSENT": "Operator Absent",
    "SCHEDULED_MAINTENANCE": "Scheduled Maintenance",
    "OTHER": "Other Reasons",
}


def _equipment(row) -> str:
    code, name = row.get("equipment_code") or "", row.get("equipment_name") or ""
    return f"{name} ({code})" if code and name else (name or code)


def _reason(row) -> str:
    category = row.get("reason_category_display") or ""
    reason = row.get("reason") or ""
    if category and reason:
        return f"{category}: {reason}"
    return category or reason


def _reports(row) -> str:
    return ", ".join(r.get("name") or "" for r in row.get("service_reports") or [])


def _person(row, prefix: str) -> str:
    name = row.get(f"{prefix}_name") or ""
    role = row.get(f"{prefix}_role_display") or ""
    return f"{name} ({role})" if name and role else (name or role)


def _procurement(row) -> str:
    return ", ".join(r.get("number") or "" for r in row.get("procurement_requests") or [])


@register("disruption-history")
def disruption_history(request):
    params = request.query_params.copy()
    params.pop("show_deleted", None)
    params["no_summary"] = "1"
    rows, _ = collect_rows(request, "equipment-disruptions-list", params=params)
    summary_params = request.query_params.copy()
    summary_params.pop("show_deleted", None)
    summary_params["limit"] = "1"
    summary = (call_view(request, "equipment-disruptions-list", summary_params) or {}).get("summary") or {}

    columns = [
        SNO,
        C("equipment", "Equipment", width=1.8, value=_equipment),
        C("department_name", "Department", width=1.1),
        C("disruption_type_display", "Type", width=1.0),
        C("scope_display", "Scope", width=0.9),
        C("start_at", "Start (IST)", spec.DATETIME, 1.1),
        C("end_at", "End (IST)", spec.DATETIME, 1.1),
        C("duration_hours", "Duration (h)", spec.NUMBER, 0.7, total=True),
        C("slots_affected", "Slots", spec.INTEGER, 0.5, total=True),
        C("bookings_affected", "Bookings", spec.INTEGER, 0.6, total=True),
        C("reason", "Reason", width=2.0, value=_reason),
        C("action_taken", "Action taken", width=2.0),
        C("service_reports", "Service report", width=1.0, value=_reports),
        C("started_by_name", "Started by", width=1.1, value=lambda r: _person(r, "started_by")),
        C("started_at", "Started at (IST)", spec.DATETIME, 1.1),
        C("ended_by_name", "Ended by", width=1.1, value=lambda r: _person(r, "ended_by")),
        C("ended_at", "Ended at (IST)", spec.DATETIME, 1.1),
        C("recovery_text", "Expected recovery", width=1.2),
        C("procurement_requests", "Procurement request", width=1.0, value=_procurement),
        C("status", "Status", width=0.6, value=lambda r: "Open" if r.get("status") == "OPEN" else "Closed"),
    ]
    table = spec.Table("disruptions", "Disruption history", columns, numbered(rows), sheet_name="Disruptions")
    kpis = [
        spec.Kpi("Disruptions", summary.get("total", len(rows)), spec.INTEGER),
        spec.Kpi("Disruption hours", summary.get("total_hours", 0), spec.NUMBER),
        spec.Kpi("Open now", summary.get("open_now", 0), spec.INTEGER),
        spec.Kpi("Reason missing", summary.get("reason_missing", 0), spec.INTEGER),
    ]
    for bucket in summary.get("by_type") or []:
        kpis.append(spec.Kpi(bucket.get("label") or "", bucket.get("count", 0), spec.INTEGER,
                             f"{float(bucket.get('hours') or 0):.2f} h"))
    filters = filter_pairs(request, [
        ("date_from", "From", "date"),
        ("date_to", "To", "date"),
        ("equipment", "Equipment", "equipment"),
        ("department", "Department", "department"),
        ("type", "Type", TYPE_LABELS),
        ("status", "Status", {"OPEN": "Open", "CLOSED": "Closed", "open": "Open", "closed": "Closed"}),
        ("reason_missing", "Reason missing", lambda raw: "Yes"),
        ("action_missing", "Action missing", lambda raw: "Yes"),
        ("source", "Recorded from", "text"),
        ("search", "Search", "text"),
    ])
    return make_document(
        request,
        title="Disruption history",
        slug="disruption-history",
        tables=[table],
        filters=filters,
        kpis=kpis,
        landscape=True,
    )
