"""Download the View Booking / My Bookings list as xlsx, csv or pdf.

Rows are every booking the page would list for the same filters, search, sort and role scope
(the caller passes the ``_booking_list_queryset`` result). Columns follow what each page shows:
``staff`` is View Booking (/booking-management), ``my`` is My Bookings.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation
from zoneinfo import ZoneInfo

from django.db.models import Prefetch
from django.http import HttpResponse
from django.utils import timezone

from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user_type import UserType

from .models import Booking
from .models import BookingSampleTrace
from .models import BookingSlotRange
from .models import BookingStatus
from .models import DailySlot
from .models import WaitlistEntry

EXPORT_ROW_LIMIT = 10_000
IST = ZoneInfo("Asia/Kolkata")
PORTAL_HEADER = "Institute Instrumentation Centre (IIC), IIT Roorkee"

# Lab Operators see the job sheet (no charges) on View Booking; OIC / Department Admin / Admin see Total Cost.
STAFF_AMOUNT_USER_TYPES = frozenset({UserType.MANAGER, UserType.DEPT_ADMIN, UserType.ADMIN})

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}

_SORT_LABELS = {
    "booking_ref": "Booking ID",
    "booking_id": "Booking ID",
    "equipment_name": "Equipment",
    "user_name": "User name",
    "supervisor_name": "Supervisor name",
    "user_phone": "User mobile",
    "user_email": "User email",
    "start_time": "Start",
    "end_time": "End",
    "duration": "Duration",
    "total_time_minutes": "Duration",
    "total_charge": "Cost",
    "status": "Status",
    "created_at": "Booked on",
    "updated_at": "Last updated",
    "rating": "Rating",
}


class ExportTooLarge(Exception):
    pass


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    pdf_width: float  # relative share of the PDF table width
    kind: str = "text"  # text | int | money | datetime


def export_columns(view: str, user) -> list[Column]:
    if view == "staff":
        cols = [
            Column("sno", "S.No", 0.6, "int"),
            Column("booking_id", "Booking ID", 2.3),
            Column("equipment", "Equipment", 1.9),
            Column("user", "User", 1.5),
            Column("user_type", "User type", 1.0),
            Column("department", "Department", 1.3),
            Column("supervisor", "Supervisor", 1.4),
            Column("mobile", "Mobile", 1.6),
            Column("email", "Email", 2.2),
            Column("slot_dates", "Slot date(s)", 1.5),
            Column("slot_times", "Slot time(s) (IST)", 1.5),
            Column("duration", "Duration", 0.8),
            Column("status", "Status", 1.3),
            Column("samples", "Samples", 0.9),
            Column("sample_status", "Sample status", 1.1),
        ]
        if getattr(user, "user_type", None) in STAFF_AMOUNT_USER_TYPES:
            cols.append(Column("amount", "Amount (₹)", 1.0, "money"))
        cols.append(Column("booked_on", "Booked on (IST)", 1.3, "datetime"))
        return cols
    return [
        Column("sno", "S.No", 0.6, "int"),
        Column("booking_id", "Booking ID", 2.2),
        Column("equipment", "Equipment", 2.6),
        Column("user", "User", 1.8),
        Column("slot_dates", "Slot date(s)", 1.8),
        Column("slot_times", "Slot time(s) (IST)", 1.8),
        Column("duration", "Duration", 1.0),
        Column("amount", "Amount (₹)", 1.1, "money"),
        Column("status", "Status", 2.0),
        Column("samples", "Samples", 1.1),
        Column("booked_on", "Booked on (IST)", 1.5, "datetime"),
    ]


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------


def _ist(dt):
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt.astimezone(IST)


def format_ist_date(dt) -> str:
    local = _ist(dt)
    return local.strftime("%d-%m-%Y") if local else ""


def format_ist_datetime(dt) -> str:
    local = _ist(dt)
    return local.strftime("%d-%m-%Y %H:%M") if local else ""


def format_duration(minutes) -> str:
    try:
        total = int(minutes or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    if total < 60:
        return f"{total} min"
    hours, mins = divmod(total, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def slot_summary(ranges) -> tuple[str, str]:
    """(dates, times) text for a booking's slot ranges, in IST; adjacent slots on a day merge into one range."""
    local = sorted((_ist(s), _ist(e)) for s, e in ranges if s is not None and e is not None)
    if not local:
        return "", ""
    merged: list[list] = []
    for start, end in local:
        if merged and merged[-1][1] == start and merged[-1][0].date() == start.date():
            merged[-1][1] = end
        else:
            merged.append([start, end])
    dates: list[str] = []
    times: list[str] = []
    for start, end in merged:
        d = start.strftime("%d-%m-%Y")
        if d not in dates:
            dates.append(d)
        t = f"{start:%H:%M}–{end:%H:%M}"
        if t not in times:
            times.append(t)
    date_text = ", ".join(dates) if len(dates) <= 3 else f"{dates[0]} to {dates[-1]} ({len(dates)} days)"
    time_text = ", ".join(times[:4]) + (f" (+{len(times) - 4} more)" if len(times) > 4 else "")
    return date_text, time_text


def _money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _wallet_owner_names(users) -> dict:
    """Supervisor (wallet owner) label per user id, same rule as the list's Supervisor Name column."""
    from iic_booking.users.models.wallet import WalletJoinRequest
    from iic_booking.users.models.wallet import WalletJoinRequestStatus

    names = {u.pk: None for u in users}
    student_ids = [u.pk for u in users if u.user_type in {UserType.STUDENT, UserType.OTHER}]
    if not student_ids:
        return names
    seen = set()
    qs = WalletJoinRequest.objects.filter(student_id__in=student_ids, status=WalletJoinRequestStatus.APPROVED)
    if not qs.ordered:
        qs = qs.order_by("pk")
    for req in qs.select_related("wallet__user"):
        if req.student_id in seen:
            continue
        seen.add(req.student_id)
        owner = getattr(getattr(req, "wallet", None), "user", None) if req.wallet_id else None
        if owner is not None and owner.pk != req.student_id:
            names[req.student_id] = get_user_display_name(owner)
    return names


def _load_bookings(queryset) -> list:
    return list(
        queryset.select_related("user", "user__department", "equipment", "charge_profile").prefetch_related(
            Prefetch(
                "daily_slots",
                queryset=DailySlot.objects.only("id", "booking_id", "start_datetime", "end_datetime").order_by(
                    "start_datetime",
                ),
            ),
            Prefetch(
                "sample_trace_events",
                queryset=BookingSampleTrace.objects.only("id", "booking_id", "status", "created_at").order_by(
                    "created_at", "id",
                ),
            ),
        ),
    )


def build_booking_rows(bookings) -> list[dict]:
    from .booking_sample_summary import SampleCountFieldIndex
    from .booking_sample_summary import booking_sample_summary
    from .serializers import _booking_status_display

    user_type_labels = dict(UserType.get_choices())
    users = {b.user.pk: b.user for b in bookings if b.user_id and b.user}
    supervisors = _wallet_owner_names(list(users.values()))
    no_slot_ids = [b.pk for b in bookings if not b.daily_slots.all()]
    released = {}
    for start_idx in range(0, len(no_slot_ids), 500):
        chunk = no_slot_ids[start_idx:start_idx + 500]
        for booking_id, start, end in BookingSlotRange.objects.filter(booking_id__in=chunk).values_list(
            "booking_id", "start_datetime", "end_datetime",
        ):
            released[booking_id] = (start, end)
    sample_index = SampleCountFieldIndex()
    sample_index.preload({b.equipment_id for b in bookings})

    rows = []
    for b in bookings:
        slots = [(s.start_datetime, s.end_datetime) for s in b.daily_slots.all()]
        if not slots and b.pk in released:
            slots = [released[b.pk]]
        dates, times = slot_summary(slots)
        summary = booking_sample_summary(b, sample_index) or {}
        sample_parts = []
        if (summary.get("sets") or 0) > 1:
            sample_parts.append(f"{summary['sets']} sets")
        if summary.get("samples") is not None:
            n = summary["samples"]
            n_text = int(n) if float(n).is_integer() else n
            sample_parts.append(f"{n_text} {'sample' if n == 1 else 'samples'}")
        events = list(b.sample_trace_events.all())
        user = b.user if b.user_id else None
        code = (b.user_type_snapshot or "").strip()
        rows.append(
            {
                "booking_id": booking_display_id_for_email(b),
                "equipment": getattr(b.equipment, "name", "") or "",
                "user": get_user_display_name(user) if user else "",
                "user_type": user_type_labels.get(code, code),
                "department": getattr(getattr(user, "department", None), "name", "") or "",
                "supervisor": supervisors.get(getattr(user, "pk", None)) or "",
                "mobile": getattr(user, "phone_number", "") or "",
                "email": getattr(user, "email", "") or "",
                "slot_dates": dates,
                "slot_times": times,
                "duration": format_duration(b.total_time_minutes),
                "status": _booking_status_display(b),
                "samples": " · ".join(sample_parts),
                "sample_status": events[-1].get_status_display() if events else "",
                "amount": _money(b.total_charge),
                "booked_on": b.created_at,
            },
        )
    return rows


def _waitlist_entries(request, *, include: bool):
    """Active waitlist entries My Bookings lists with the bookings, narrowed by the same filters."""
    if not include:
        return []
    params = request.query_params
    if (params.get("start_date") or "").strip() or (params.get("end_date") or "").strip():
        return []
    if str(params.get("results_overdue") or "").strip().lower() in ("1", "true", "yes"):
        return []
    qs = WaitlistEntry.objects.filter(user=request.user, status="ACTIVE").select_related(
        "equipment", "equipment__internal_department", "user",
    )
    equipment_id = (params.get("equipment_id") or "").strip()
    if equipment_id:
        try:
            qs = qs.filter(equipment_id=int(equipment_id))
        except (TypeError, ValueError):
            return []
    return list(qs.order_by("-created_at"))


def build_waitlist_rows(entries, search: str) -> list[dict]:
    from .waitlist import active_waitlist_position
    from .waitlist import waitlist_virtual_booking_id

    rows = []
    for entry in entries:
        position = active_waitlist_position(entry) or 0
        equipment = entry.equipment
        display_id = waitlist_virtual_booking_id(
            getattr(equipment, "code", "") or "",
            position,
            department_code=Booking.department_code_for_virtual_id(equipment),
            created_at=entry.created_at,
        )
        name = getattr(equipment, "name", "") or ""
        if search:
            needle = search.lower()
            if needle not in name.lower() and needle not in display_id.lower():
                continue
        queue = f"WL{position}" if position else "queue"
        rows.append(
            {
                "booking_id": display_id,
                "equipment": name,
                "user": get_user_display_name(entry.user),
                "user_type": "",
                "department": "",
                "supervisor": "",
                "mobile": "",
                "email": "",
                "slot_dates": "",
                "slot_times": "",
                "duration": "",
                "status": f"Waitlisted ({queue}, not a confirmed booking)",
                "samples": "",
                "sample_status": "",
                "amount": None,
                "booked_on": entry.created_at,
            },
        )
    return rows


# ---------------------------------------------------------------------------
# Filters summary / file naming
# ---------------------------------------------------------------------------


def filters_summary(request, *, view: str) -> list[tuple[str, str]]:
    from .models import Equipment

    params = request.query_params

    def text(name):
        value = (params.get(name) or "").strip()
        return value if len(value) >= 2 else ""

    out: list[tuple[str, str]] = []
    status_value = (params.get("status") or "").strip().upper()
    if str(params.get("results_overdue") or "").strip().lower() in ("1", "true", "yes"):
        out.append(("Status", "Results overdue"))
    elif status_value:
        out.append(("Status", dict(BookingStatus.choices).get(status_value, status_value)))
    else:
        out.append(("Status", "All"))
    if text("search"):
        out.append(("Search", text("search")))
    if (params.get("start_date") or "").strip():
        out.append(("From", _iso_to_dmy(params.get("start_date"))))
    if (params.get("end_date") or "").strip():
        out.append(("To", _iso_to_dmy(params.get("end_date"))))
    equipment_id = (params.get("equipment_id") or "").strip()
    if equipment_id.isdigit():
        name = Equipment.objects.filter(pk=int(equipment_id)).values_list("name", flat=True).first()
        out.append(("Equipment", name or equipment_id))
    if text("user_name"):
        out.append(("User name", text("user_name")))
    if text("supervisor_name"):
        out.append(("Supervisor name", text("supervisor_name")))
    user_type = (params.get("user_type_filter") or "").strip().lower()
    if user_type in ("internal", "external"):
        out.append(("User type", "Internal (students / faculty)" if user_type == "internal" else "External"))
    istem = (params.get("istem_fbr") or "").strip().lower()
    if istem in ("verified", "unverified"):
        out.append(("I-STEM FBR", istem.capitalize()))
    ordering = (params.get("ordering") or "").strip()
    key = ordering.lstrip("-")
    if key in _SORT_LABELS:
        out.append(("Sorted by", f"{_SORT_LABELS[key]} ({'descending' if ordering.startswith('-') else 'ascending'})"))
    return out


def _iso_to_dmy(value) -> str:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return str(value)


def export_filename(ext: str, now=None) -> str:
    local = _ist(now or timezone.now())
    return f"bookings_{local:%Y-%m-%d_%H%M}.{ext}"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def guard_formula(value):
    """Spreadsheet formula-injection guard: text starting with = + - @ (or tab / CR) gets a leading apostrophe."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _cell_text(col: Column, value) -> str:
    if value is None or value == "":
        return ""
    if col.kind == "money":
        return f"{value:.2f}"
    if col.kind == "datetime":
        return format_ist_datetime(value)
    return str(value)


def render_csv(columns, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([c.label for c in columns])
    for row in rows:
        writer.writerow(
            [_cell_text(c, row.get(c.key)) if c.kind != "text" else guard_formula(_cell_text(c, row.get(c.key)))
             for c in columns],
        )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def render_xlsx(columns, rows, *, summary, generated_at) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment
    from openpyxl.styles import Font
    from openpyxl.styles import PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Bookings")
    ws.freeze_panes = "A2"

    widths = [len(c.label) for c in columns]
    for row in rows:
        for i, c in enumerate(columns):
            text = _cell_text(c, row.get(c.key))
            widths[i] = max(widths[i], len(text))
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="153F79", end_color="153F79", fill_type="solid")
    header = []
    for c in columns:
        cell = WriteOnlyCell(ws, value=c.label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        header.append(cell)
    ws.append(header)

    for row in rows:
        out = []
        for c in columns:
            value = row.get(c.key)
            if c.kind == "datetime" and value is not None:
                cell = WriteOnlyCell(ws, value=_ist(value).replace(tzinfo=None))
                cell.number_format = "DD-MM-YYYY HH:MM"
            elif c.kind == "money" and value is not None:
                cell = WriteOnlyCell(ws, value=float(value))
                cell.number_format = "#,##0.00"
            elif c.kind == "int":
                cell = WriteOnlyCell(ws, value=value)
            else:
                cell = WriteOnlyCell(ws, value=guard_formula(_cell_text(c, value)) or None)
            out.append(cell)
        ws.append(out)

    info = wb.create_sheet("Filters")
    info.column_dimensions["A"].width = 18
    info.column_dimensions["B"].width = 60
    bold = Font(bold=True)
    title = WriteOnlyCell(info, value=f"Bookings — {PORTAL_HEADER}")
    title.font = Font(bold=True, size=12)
    info.append([title])
    for label, value in [("Generated at", generated_at), ("Rows", str(len(rows))), *summary]:
        key_cell = WriteOnlyCell(info, value=label)
        key_cell.font = bold
        info.append([key_cell, guard_formula(value)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit_lines(text: str, font: str, size: float, width: float) -> str:
    """Word-wrap ``text`` to ``width`` points, breaking single words (e.g. emails) that are still too wide."""
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase.pdfmetrics import stringWidth

    lines = []
    for line in simpleSplit(text, font, size, width):
        while len(line) > 1 and stringWidth(line, font, size) > width:
            cut = len(line) - 1
            while cut > 1 and stringWidth(line[:cut], font, size) > width:
                cut -= 1
            lines.append(line[:cut])
            line = line[cut:]
        lines.append(line)
    return "\n".join(lines)


def render_pdf(columns, rows, *, summary, generated_at) -> bytes:
    from django.conf import settings
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Paragraph
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.platypus import Spacer
    from reportlab.platypus import Table
    from reportlab.platypus import TableStyle

    from .document_exports import _pdf_letterhead_story_lines
    from .document_exports import _register_pdf_rupee_font

    unicode_font = _register_pdf_rupee_font()
    body_font = unicode_font or "Helvetica"
    header_font = unicode_font or "Helvetica-Bold"
    font_size = 6.5

    class _NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self.setFont("Helvetica", 7)
                self.setFillColor(colors.HexColor("#64748b"))
                width, _ = self._pagesize
                self.drawString(1 * cm, 0.6 * cm, f"Bookings — generated {generated_at}")
                self.drawRightString(width - 1 * cm, 0.6 * cm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1 * cm,
        rightMargin=1 * cm,
        topMargin=1 * cm,
        bottomMargin=1.2 * cm,
        title="Bookings",
        author=PORTAL_HEADER,
    )
    styles = getSampleStyleSheet()
    meta_style = ParagraphStyle(
        "export_meta", parent=styles["Normal"], fontName=body_font, fontSize=8, leading=10, alignment=TA_CENTER,
    )
    dept = getattr(settings, "ORG_DEPARTMENT_NAME", "") or "Institute Instrumentation Centre (IIC)"
    story = list(_pdf_letterhead_story_lines(department_name=dept, document_title="Bookings"))
    applied = "; ".join(f"{label}: {value}" for label, value in summary) or "None"
    story.append(Paragraph(_escape(f"Filters — {applied}"), meta_style))
    story.append(
        Paragraph(_escape(f"Generated at {generated_at} IST · {len(rows)} booking{'s' if len(rows) != 1 else ''}"),
                  meta_style),
    )
    story.append(Spacer(1, 0.3 * cm))

    total_share = sum(c.pdf_width for c in columns)
    col_widths = [doc.width * c.pdf_width / total_share for c in columns]
    rupee = "₹" if unicode_font else "Rs."

    def header_text(c: Column, width: float) -> str:
        return _fit_lines(c.label.replace("₹", rupee), header_font, font_size, width - 4)

    def wrap(text: str, width: float) -> str:
        if not text:
            return ""
        if not unicode_font:
            text = text.replace("₹", "Rs.").replace("–", "-").replace("·", "-")
        return _fit_lines(text, body_font, font_size, width - 4)

    header_row = [header_text(c, w) for c, w in zip(columns, col_widths)]
    money_cols = [i for i, c in enumerate(columns) if c.kind == "money"]
    style = [
        ("FONTNAME", (0, 0), (-1, -1), body_font),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 1.5),
        ("FONTNAME", (0, 0), (-1, 0), header_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#153f79")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#94a3b8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
    ]
    for i in money_cols:
        style.append(("ALIGN", (i, 1), (i, -1), "RIGHT"))

    if not rows:
        story.append(Paragraph("No bookings match these filters.", meta_style))
    # Large tables split slowly in reportlab; chunks keep rendering linear and still repeat the header.
    chunk = 250
    for start in range(0, len(rows), chunk):
        data = [header_row]
        for row in rows[start:start + chunk]:
            data.append([wrap(_cell_text(c, row.get(c.key)), w) for c, w in zip(columns, col_widths)])
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle(style))
        story.append(table)

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def export_booking_list(request, queryset, *, view: str, export_format: str) -> HttpResponse:
    from .api_views import _user_is_accounts_finance_user

    params = request.query_params
    status_value = (params.get("status") or "").strip().upper()
    include_waitlist = (
        view == "my"
        and not _user_is_accounts_finance_user(request.user)
        and status_value in ("", BookingStatus.WAITLISTED)
    )
    entries = _waitlist_entries(request, include=include_waitlist)

    total = queryset.count()
    if total + len(entries) > EXPORT_ROW_LIMIT:
        raise ExportTooLarge(
            f"{total + len(entries):,} bookings match these filters; exports are limited to "
            f"{EXPORT_ROW_LIMIT:,}. Narrow the filters (for example a date range) and try again.",
        )

    rows = build_booking_rows(_load_bookings(queryset))
    if entries:
        search = (params.get("search") or "").strip()
        waitlist_rows = build_waitlist_rows(entries, search if len(search) >= 2 else "")
        ordering = (params.get("ordering") or "-created_at").strip()
        rows = rows + waitlist_rows
        # My Bookings interleaves waitlist entries by date only for the default created-at sorts.
        if ordering in ("created_at", "-created_at"):
            rows.sort(key=lambda r: r["booked_on"] or timezone.now(), reverse=ordering.startswith("-"))
    for index, row in enumerate(rows, start=1):
        row["sno"] = index

    columns = export_columns(view, request.user)
    now = timezone.now()
    generated_at = format_ist_datetime(now)
    summary = filters_summary(request, view=view)
    if export_format == "csv":
        content = render_csv(columns, rows)
    elif export_format == "xlsx":
        content = render_xlsx(columns, rows, summary=summary, generated_at=generated_at)
    else:
        content = render_pdf(columns, rows, summary=summary, generated_at=generated_at)

    response = HttpResponse(content, content_type=_CONTENT_TYPES[export_format])
    response["Content-Disposition"] = f'attachment; filename="{export_filename(export_format, now)}"'
    response["X-Export-Row-Count"] = str(len(rows))
    response["Cache-Control"] = "no-store"
    return response
