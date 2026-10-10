"""Helpers for report builders: readable filter summaries and document defaults."""

from __future__ import annotations

from datetime import datetime

from .. import spec
from ..http import role_label


def _date_label(raw: str) -> str:
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return raw


def _equipment_label(raw: str) -> str:
    from iic_booking.equipment.models import Equipment

    ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
    names = [f"{e.code} — {e.name}" if e.code else e.name for e in Equipment.objects.filter(pk__in=ids)]
    return ", ".join(names) or raw


def _department_label(raw: str) -> str:
    from iic_booking.users.models import Department

    if not str(raw).isdigit():
        return raw
    dept = Department.objects.filter(pk=int(raw)).only("name").first()
    return dept.name if dept else raw


def _user_label(raw: str) -> str:
    from iic_booking.users.display import get_user_display_name
    from iic_booking.users.models import User

    if not str(raw).isdigit():
        return raw
    user = User.objects.filter(pk=int(raw)).first()
    return get_user_display_name(user) if user else raw


RESOLVERS = {
    "date": _date_label,
    "equipment": _equipment_label,
    "department": _department_label,
    "user": _user_label,
    "text": lambda raw: raw,
}


def filter_pairs(request, fields) -> list[tuple[str, str]]:
    """``fields``: (param, label, kind) where kind is a RESOLVERS key, a dict of choice labels or a callable."""
    params = request.query_params
    out = []
    for param, label, kind in fields:
        values = [v for v in params.getlist(param) if str(v).strip()]
        if not values:
            continue
        raw = ",".join(str(v).strip() for v in values)
        if isinstance(kind, dict):
            text = ", ".join(str(kind.get(v.strip().upper(), kind.get(v.strip(), v))) for v in raw.split(","))
        elif callable(kind):
            text = kind(raw)
        else:
            text = RESOLVERS.get(kind, RESOLVERS["text"])(raw)
        out.append((label, text))
    return out


def visible_columns(columns, user) -> list[spec.Column]:
    return [c for c in columns if c.visible is None or c.visible(user)]


def make_document(request, *, title: str, slug: str, tables, subtitle: str = "", filters=None, kpis=None,
                  landscape=None) -> spec.Document:
    for table in tables:
        table.columns = visible_columns(table.columns, request.user)
    return spec.Document(
        title=title,
        slug=slug,
        tables=list(tables),
        subtitle=subtitle,
        filters=list(filters or []),
        kpis=list(kpis or []),
        generated_by=role_label(request.user),
        landscape=landscape,
    )


def numbered(rows: list[dict]) -> list[dict]:
    for index, row in enumerate(rows, start=1):
        row["_sno"] = index
    return rows


SNO = spec.Column("_sno", "S.No.", spec.INTEGER, 0.45)


def booking_link(request, *, display_key: str = "booking_id", pk_key: str = "real_booking_id"):
    """Column ``link`` for a Booking ID cell: Booking Management for staff, My Bookings for everyone else."""
    from iic_booking.equipment.booking_links import booking_detail_path
    from iic_booking.equipment.results_deadline import viewer_is_staff

    staff = viewer_is_staff(request.user)

    def link(row: dict) -> str:
        pk = row.get(pk_key)
        return booking_detail_path(pk=pk if str(pk or "").isdigit() else None,
                                   display_id=str(row.get(display_key) or ""), staff=staff)

    return link
