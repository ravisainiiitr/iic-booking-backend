"""Dashboard insight pages: equipment overview, users overview and cancellations."""

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


def _equipment_path(key: str = "equipment_id"):
    return lambda row: f"/equipment/{row[key]}" if row.get(key) else None


def _kpis_from(breakdown, kind=spec.INTEGER) -> list[spec.Kpi]:
    return [spec.Kpi(item.get("label") or "", item.get("count", 0), kind) for item in breakdown or []]


@register("admin-equipment-overview")
def equipment_overview(request):
    rows, first = collect_rows(request, "admin-insights-equipment", page_param="page", limit_param="page_size")
    summary = (first or {}).get("summary") or {}
    for row in rows:
        row["category_name"] = (row.get("category") or {}).get("name") or ""
        row["department_name"] = (row.get("department") or {}).get("name") or ""
        row["oic_names"] = ", ".join(o.get("name") or "" for o in row.get("officers_in_charge") or [])
    columns = [
        SNO,
        C("name", "Equipment", width=1.8, link=_equipment_path(), align="left"),
        C("code", "Code", width=0.9),
        C("status_display", "Status", width=0.9),
        C("category_name", "Category", width=1.1),
        C("department_name", "Department", width=1.1),
        C("oic_names", "Officer in Charge", width=1.4),
        C("profile_type_display", "Profile", width=0.9),
        C("down_since", "Down since (IST)", spec.DATETIME, 1.1),
        C("downtime_hours", "Down for (h)", spec.NUMBER, 0.7),
        C("last_status_change", "Last status change (IST)", spec.DATETIME, 1.1),
        C("upcoming_bookings", "Upcoming bookings", spec.INTEGER, 0.7, total=True),
        C("utilisation", "Utilisation (30 days)", spec.PERCENT, 0.8),
        C("test_only", "Test only", spec.BOOL, 0.5),
    ]
    table = spec.Table("equipment", "Equipment", columns, numbered(rows), sheet_name="Equipment")
    kpis = [spec.Kpi("Equipment", summary.get("total", len(rows)), spec.INTEGER)]
    kpis += _kpis_from(summary.get("by_status"))
    if summary.get("utilisation") is not None:
        kpis.append(spec.Kpi("Utilisation (30 days)", summary["utilisation"], spec.PERCENT, "Booked ÷ slot hours"))
    breakdowns = [
        spec.Table(
            key,
            title,
            [SNO, C("label", label, width=2.0, align="left"), C("count", "Equipment", spec.INTEGER, 0.8, total=True)],
            numbered([dict(item) for item in summary.get(key) or []]),
            sheet_name=title[:31],
        )
        for key, title, label in (
            ("by_category", "By category", "Category"),
            ("by_department", "By department", "Department"),
            ("by_oic", "By Officer in Charge", "Officer in Charge"),
            ("by_profile_type", "By profile type", "Profile type"),
        )
    ]
    filters = filter_pairs(request, [
        ("status", "Status", {"OPERATIONAL": "Operational", "UNDER_MAINTENANCE": "Under maintenance",
                              "OTHER": "Other", "DISPOSED": "Disposed", "ALL": "All"}),
        ("category", "Category", "text"),
        ("department", "Department", "department"),
        ("oic", "Officer in Charge", "user"),
        ("profile_type", "Profile type", "text"),
        ("search", "Search", "text"),
    ])
    return make_document(
        request, title="Equipment overview", slug="equipment-overview", tables=[table, *breakdowns],
        filters=filters, kpis=kpis, landscape=True,
    )


@register("admin-users-overview")
def users_overview(request):
    rows, first = collect_rows(request, "admin-insights-users", page_param="page", limit_param="page_size")
    summary = (first or {}).get("summary") or {}
    for row in rows:
        row["department_name"] = (row.get("department") or {}).get("name") or ""
    columns = [
        SNO,
        C("name", "Name", width=1.6, align="left"),
        C("category_display", "Category", width=1.1),
        C("programme_display", "Programme", width=0.9),
        C("department_name", "Department / organisation", width=1.6),
        C("email", "Email", width=1.8),
        C("phone", "Mobile", width=1.0),
        C("date_joined", "Joined (IST)", spec.DATETIME, 1.0),
        C("last_booking_at", "Last booking (IST)", spec.DATETIME, 1.0),
        C("bookings_count", "Bookings", spec.INTEGER, 0.6, total=True),
        C("is_active", "Active", spec.BOOL, 0.5),
        C("wallet_owner_id", "Wallet", width=0.6,
          value=lambda r: "Open" if r.get("wallet_owner_id") else "",
          link=lambda r: f"/admin/wallet-ledger/{r['wallet_owner_id']}" if r.get("wallet_owner_id") else None),
    ]
    table = spec.Table("users", "Users", columns, numbered(rows), sheet_name="Users")
    kpis = [
        spec.Kpi("Users", summary.get("total", len(rows)), spec.INTEGER),
        spec.Kpi("Active", summary.get("active", 0), spec.INTEGER),
        spec.Kpi("Internal", summary.get("internal", 0), spec.INTEGER),
        spec.Kpi("External", summary.get("external", 0), spec.INTEGER),
    ]
    kpis += _kpis_from([c for c in summary.get("by_category") or [] if c.get("count")])
    categories = spec.Table(
        "by_category", "By category",
        [SNO, C("label", "Category", width=2.0, align="left"), C("count", "Users", spec.INTEGER, 0.8, total=True)],
        numbered([dict(c) for c in summary.get("by_category") or []]), sheet_name="By category",
    )
    departments = spec.Table(
        "internal_by_department", "Internal users by department",
        [SNO, C("name", "Department", width=2.0, align="left"),
         C("faculty", "Faculty", spec.INTEGER, 0.7, total=True),
         C("students", "Students", spec.INTEGER, 0.7, total=True),
         C("staff", "Staff", spec.INTEGER, 0.7, total=True),
         C("startups", "Startups", spec.INTEGER, 0.7, total=True),
         C("total", "Total", spec.INTEGER, 0.7, total=True)],
        numbered([dict(d) for d in summary.get("internal_by_department") or []]), sheet_name="By department",
    )
    organisations = spec.Table(
        "external_by_organisation", "External users by organisation (top 50)",
        [SNO, C("name", "Organisation", width=2.0, align="left"), C("type", "Type", width=1.1),
         C("state", "State", width=1.0), C("count", "Users", spec.INTEGER, 0.7, total=True)],
        numbered([dict(o) for o in summary.get("external_by_organisation") or []]), sheet_name="By organisation",
    )
    filters = filter_pairs(request, [
        ("status", "Status", {"ACTIVE": "Active", "INACTIVE": "Inactive", "ALL": "All"}),
        ("segment", "Internal / external", {"INTERNAL": "Internal", "EXTERNAL": "External"}),
        ("category", "Category", {k.upper(): v for k, v in _user_categories().items()}),
        ("programme", "Programme", "text"),
        ("department", "Department", "department"),
        ("organisation", "Organisation", "text"),
        ("joined_from", "Joined from", "date"),
        ("joined_to", "Joined to", "date"),
        ("booked_from", "Booked from", "date"),
        ("booked_to", "Booked to", "date"),
        ("search", "Search", "text"),
    ])
    return make_document(
        request, title="Users overview", slug="users-overview",
        tables=[table, categories, departments, organisations], filters=filters, kpis=kpis, landscape=True,
    )


def _user_categories() -> dict[str, str]:
    from iic_booking.equipment.admin_insights.users import CATEGORIES

    return CATEGORIES


@register("admin-cancellations")
def cancellations(request):
    rows, first = collect_rows(request, "admin-insights-cancellations", page_param="page", limit_param="page_size")
    data = first or {}
    summary = data.get("summary") or {}
    for row in rows:
        booking = row.get("booking") or {}
        user = row.get("user") or {}
        equipment = row.get("equipment") or {}
        row["booking_display_id"] = booking.get("display_id") or ""
        row["booking_pk"] = booking.get("pk")
        row["user_name"] = user.get("name") or ""
        row["user_category"] = user.get("category_display") or ""
        row["user_department"] = user.get("department") or ""
        row["equipment_id"] = equipment.get("id")
        row["equipment_label"] = (
            f"{equipment.get('name')} ({equipment.get('code')})" if equipment.get("code") else equipment.get("name") or ""
        )
        row["lead_hours"] = round(row["lead_minutes"] / 60, 1) if row.get("lead_minutes") is not None else None
        row["by"] = " — ".join(x for x in (row.get("actor_role_display"), row.get("cancelled_by")) if x)
    columns = [
        SNO,
        C("booking_display_id", "Booking ID", width=1.3,
          link=booking_link(request, display_key="booking_display_id", pk_key="booking_pk")),
        C("user_name", "User", width=1.4, align="left"),
        C("user_category", "Category", width=1.0),
        C("user_department", "Department / organisation", width=1.3),
        C("equipment_label", "Equipment", width=1.6, link=_equipment_path(), align="left"),
        C("slot_start", "Slot (IST)", spec.DATETIME, 1.1),
        C("cancelled_at", "Cancelled at (IST)", spec.DATETIME, 1.1),
        C("by", "Cancelled by", width=1.3),
        C("reason_display", "Reason", width=1.4),
        C("note", "Notes", width=1.6, align="left"),
        C("lead_hours", "Lead time (h)", spec.NUMBER, 0.7),
        C("late", "Late (<24 h)", spec.BOOL, 0.6),
        C("charge", "Charge (₹)", spec.CURRENCY, 0.8, total=True),
        C("refund", "Refund (₹)", spec.CURRENCY, 0.8, total=True),
        C("refund_estimated", "Refund estimated", spec.BOOL, 0.6),
        C("refill_display", "Slots re-booked", width=1.1),
        C("data_quality_display", "Data", width=1.0),
    ]
    table = spec.Table("cancellations", "Cancellations", columns, numbered(rows), sheet_name="Cancellations")
    kpis = [
        spec.Kpi("Cancellations", summary.get("total", len(rows)), spec.INTEGER),
        spec.Kpi("Of bookings created", summary.get("rate") or 0, spec.PERCENT,
                 f"{summary.get('bookings_created', 0)} bookings created"),
        spec.Kpi("Late (<24 h)", summary.get("late", 0), spec.INTEGER),
        spec.Kpi("Refunded", summary.get("refund_total", 0), spec.CURRENCY),
        spec.Kpi("Charges retained", summary.get("retained_total", 0), spec.CURRENCY),
        spec.Kpi("Previous period", (summary.get("previous") or {}).get("total", 0), spec.INTEGER),
    ]
    breakdowns = [
        spec.Table(
            key, title,
            [SNO, C("label", label, width=2.0, align="left"),
             C("count", "Cancellations", spec.INTEGER, 0.8, total=True),
             C("late", "Late", spec.INTEGER, 0.6, total=True)],
            numbered([dict(item) for item in summary.get(key) or []]), sheet_name=title[:31],
        )
        for key, title, label in (
            ("by_role", "By who cancelled", "Cancelled by"),
            ("by_reason", "By reason", "Reason"),
            ("by_lead_time", "By lead time", "Lead time"),
            ("by_equipment", "By equipment (top 15)", "Equipment"),
            ("by_department", "By department (top 15)", "Department"),
            ("by_category", "By user category", "Category"),
            ("by_oic", "By Officer in Charge (top 15)", "Officer in Charge"),
            ("by_data_quality", "By data quality", "Data"),
        )
    ]
    filters = [("Dates", f"{data.get('date_from', '')} to {data.get('date_to', '')}")]
    filters += filter_pairs(request, [
        ("equipment", "Equipment", "equipment"),
        ("role", "Cancelled by", "text"),
        ("reason", "Reason", "text"),
        ("department", "Department", "department"),
        ("category", "User category", "text"),
        ("oic", "Officer in Charge", "user"),
        ("late_only", "Late only", lambda raw: "Yes"),
        ("include_no_shows", "Including no-shows", lambda raw: "Yes"),
        ("search", "Search", "text"),
    ])
    return make_document(
        request, title="Cancellations", slug="cancellations", tables=[table, *breakdowns],
        filters=filters, kpis=kpis, landscape=True,
    )
