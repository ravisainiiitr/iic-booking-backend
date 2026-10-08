"""People-related lists: equipment operating nominations and OIC substitute assignments."""

from __future__ import annotations

from .. import spec
from ..bridge import collect_rows
from ..registry import register
from .booking_activity import humanize
from .common import SNO
from .common import filter_pairs
from .common import make_document
from .common import numbered

C = spec.Column


def _nomination_statuses() -> dict:
    from iic_booking.equipment.models import StudentEquipmentNominationStatus

    return {str(k): str(v) for k, v in StudentEquipmentNominationStatus.choices}


def _equipment(row) -> str:
    code, name = row.get("equipment_code") or "", row.get("equipment_name") or ""
    return f"{name} ({code})" if code and name else (name or code)


def _programme(row) -> str:
    bits = [row.get("student_degree_name") or "", row.get("student_branch_name") or ""]
    text = " · ".join(b for b in bits if b)
    dept = row.get("student_department_name") or ""
    return f"{text}\n{dept}" if text and dept else (text or dept)


def _semester(row) -> str:
    return row.get("academic_year_name") or row.get("semester_name") or row.get("semester_code") or ""


def _nomination_columns(statuses, *, student=True, supervisor=True, outcome=True) -> list:
    columns = [SNO]
    if student:
        columns += [
            C("student_name", "Student", width=1.3),
            C("student_email", "Email", width=1.6),
            C("programme", "Programme", width=1.5, value=_programme),
        ]
    if supervisor:
        columns.append(C("supervisor_name", "Supervisor", width=1.3))
    columns += [
        C("equipment", "Equipment", width=1.6, value=_equipment),
        C("semester", "Semester", width=1.0, value=_semester),
        C("status", "Status", width=0.8,
          value=lambda r: r.get("status_display") or statuses.get(r.get("status"), humanize(r.get("status")))),
    ]
    if outcome:
        columns.append(C("outcome_summary", "Outcome", width=1.6))
    columns += [
        C("nominated_at", "Nominated at (IST)", spec.DATETIME, 1.15),
        C("approved_by_name", "Decided by", width=1.1),
        C("approved_at", "Decided at (IST)", spec.DATETIME, 1.15),
        C("remarks", "Remarks", width=1.5),
    ]
    return columns


def _nomination_filters(request, statuses) -> list:
    from iic_booking.equipment.models import Semester

    def semester(raw: str) -> str:
        if not raw.isdigit():
            return raw
        sem = Semester.objects.filter(pk=int(raw)).first()
        return str(getattr(sem, "name", "") or getattr(sem, "code", "") or raw) if sem else raw

    return filter_pairs(request, [
        ("semester_id", "Semester", semester),
        ("semester", "Semester", semester),
        ("status", "Status", statuses),
        ("equipment_id", "Equipment", "equipment"),
        ("supervisor_id", "Supervisor", "user"),
    ])


@register("student-equipment-nominations")
def admin_student_nominations(request):
    statuses = _nomination_statuses()
    rows, _ = collect_rows(request, "admin-student-equipment-nominations-list", results_key="results",
                           page_size=None)
    table = spec.Table("nominations", "Student equipment operating nominations",
                       _nomination_columns(statuses, outcome=False), numbered(rows),
                       empty_message="No nominations match these filters.")
    return make_document(request, title="Student Equipment Operating Nominations",
                         slug="student-equipment-nominations", tables=[table],
                         filters=_nomination_filters(request, statuses), landscape=True)


@register("ta-nominations-log")
def ta_nominations_log(request):
    statuses = _nomination_statuses()
    rows, _ = collect_rows(request, "equipment-nominations-admin-list", results_key="nominations", page_size=None)
    table = spec.Table("nominations", "Equipment operating nominations", _nomination_columns(statuses),
                       numbered(rows), empty_message="No nominations match these filters.")
    return make_document(request, title="Equipment Operating Nominations Log", slug="nominations-log",
                         tables=[table], filters=_nomination_filters(request, statuses), landscape=True)


@register("my-nominations-supervisor")
def my_nominations_supervisor(request):
    statuses = _nomination_statuses()
    rows, _ = collect_rows(request, "equipment-nominations-my-supervisor", results_key="nominations",
                           page_size=None)
    table = spec.Table("nominations", "Students I nominated", _nomination_columns(statuses, supervisor=False),
                       numbered(rows), empty_message="You have not nominated any students for these filters.")
    return make_document(request, title="My Equipment Operating Nominations", slug="my-nominations",
                         tables=[table], filters=_nomination_filters(request, statuses), landscape=True)


@register("my-nominations-student")
def my_nominations_student(request):
    statuses = _nomination_statuses()
    rows, _ = collect_rows(request, "equipment-nominations-my-student", results_key="nominations", page_size=None)
    table = spec.Table("nominations", "My nomination requests", _nomination_columns(statuses, student=False),
                       numbered(rows), empty_message="You have no nomination requests.")
    return make_document(request, title="My Nomination Requests", slug="my-nomination-requests", tables=[table])


def _person(key):
    def value(row):
        person = row.get(key) or {}
        return person.get("name") or person.get("email") or ""
    return value


def _sub_equipment(row) -> str:
    eq = row.get("equipment") or {}
    code, name = eq.get("code") or "", eq.get("name") or ""
    return f"{name} ({code})" if code and name else (name or code)


_SUBSTITUTION_COLUMNS = [
    SNO,
    C("equipment", "Equipment", width=1.6, value=_sub_equipment),
    C("primary_oic", "Officer in charge", width=1.3, value=_person("primary_oic")),
    C("substitute", "Substitute", width=1.3, value=_person("substitute")),
    C("start_display", "From", width=1.2),
    C("end_display", "Until", width=1.2),
    C("status_label", "Status", width=0.8),
    C("reason", "Reason", width=1.7),
    C("created_at", "Created at (IST)", spec.DATETIME, 1.1),
    C("created_by", "Created by", width=1.1, value=_person("created_by")),
    C("ended", "Ended / cancelled", width=1.5, value=lambda r: " — ".join(
        b for b in [_person("ended_by")(r), r.get("end_reason") or ""] if b)),
]

_HISTORY_COLUMNS = [
    SNO,
    C("created_at", "When (IST)", spec.DATETIME, 1.1),
    C("equipment", "Equipment", width=1.6),
    C("substitute", "Substitute", width=1.3),
    C("action_label", "Action", width=1.0),
    C("actor_name", "By", width=1.2),
    C("reason", "Reason", width=2.2),
]


def _history(items) -> list[dict]:
    rows = []
    for item in items:
        for event in item.get("events") or []:
            rows.append({
                **event,
                "equipment": _sub_equipment(item),
                "substitute": _person("substitute")(item),
            })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return numbered(rows)


@register("oic-substitutes")
def oic_substitutes(request):
    from ..bridge import call_view

    data = call_view(request, "oic-substitutes") or {}
    if data.get("scope") == "admin":
        items = data.get("items") or []
        tables = [spec.Table("assignments", "OIC substitute assignments", list(_SUBSTITUTION_COLUMNS),
                             numbered(list(items)), empty_message="No substitutions match these filters.")]
        filters = filter_pairs(request, [("status", "Status", lambda raw: humanize(raw)),
                                         ("search", "Search", "text")])
        if data.get("limit") and len(items) >= data["limit"]:
            tables[0].note = f"Showing the latest {data['limit']:,} substitutions, as on the page."
    else:
        granted = data.get("granted") or []
        assigned = data.get("assigned_to_me") or []
        items = granted + assigned
        tables = [
            spec.Table("granted", "Substitutes you assigned", list(_SUBSTITUTION_COLUMNS), numbered(list(granted)),
                       empty_message="You have not assigned any OIC substitutes."),
            spec.Table("assigned", "Equipment assigned to you as substitute", list(_SUBSTITUTION_COLUMNS),
                       numbered(list(assigned)), sheet_name="Assigned to you",
                       empty_message="You are not an OIC substitute for any equipment."),
        ]
        filters = []
    tables.append(spec.Table("history", "Substitution history", list(_HISTORY_COLUMNS), _history(items),
                             empty_message="No substitution activity yet."))
    return make_document(request, title="OIC Substitute — Assignments and History", slug="oic-substitutes",
                         tables=tables, filters=filters, landscape=True)


_TICKET_STATUSES = {"OPEN": "Open", "IN_PROGRESS": "In progress", "RESOLVED": "Resolved", "CLOSED": "Closed",
                    "CANCELLED": "Cancelled"}
_TICKET_SCOPES = {"MINE": "Raised by me", "ASSIGNED": "Marked to me"}


def _ticket_equipment(row) -> str:
    code, name = row.get("related_equipment_code") or "", row.get("related_equipment_name") or ""
    return f"{name} ({code})" if code and name else (name or code)


@register("tickets")
def tickets(request):
    rows, _ = collect_rows(request, "ticket-list", results_key="tickets", page_size=200)
    columns = [
        SNO,
        C("ticket_id", "Ticket", spec.INTEGER, 0.6),
        C("created_at", "Raised (IST)", spec.DATETIME, 1.15),
        C("subject", "Subject", width=2.2),
        C("ticket_type", "Type", width=1.0,
          value=lambda r: r.get("ticket_type_name") or r.get("ticket_type_display") or humanize(r.get("ticket_type"))),
        C("priority", "Priority", width=0.7, value=lambda r: r.get("priority_display") or humanize(r.get("priority"))),
        C("status", "Status", width=0.8, value=lambda r: r.get("status_display") or humanize(r.get("status"))),
        C("requester_name", "Raised by", width=1.3,
          value=lambda r: r.get("requester_name") or r.get("user_name") or ""),
        C("requester_email", "Email", width=1.6,
          value=lambda r: r.get("requester_email") or r.get("user_email") or ""),
        C("equipment", "Equipment", width=1.5, value=_ticket_equipment),
        C("assigned_to_name", "Assigned to", width=1.2),
        C("comments_count", "Replies", spec.INTEGER, 0.6),
        C("updated_at", "Last update (IST)", spec.DATETIME, 1.15),
    ]
    filters = filter_pairs(request, [("status", "Status", _TICKET_STATUSES),
                                     ("ticket_type", "Type", "text"),
                                     ("scope", "Showing", _TICKET_SCOPES),
                                     ("search", "Search", "text")])
    table = spec.Table("tickets", "Support tickets", columns, numbered(rows),
                       empty_message="No tickets match these filters.")
    return make_document(request, title="Support Tickets", slug="support-tickets", tables=[table],
                         filters=filters, landscape=True)
