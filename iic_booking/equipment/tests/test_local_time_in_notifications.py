"""Times shown to people (emails, notifications, comments, PDFs) are in IST, not the stored UTC.

Production report: the same-day reminder for a 14:00–15:30 IST slot printed "08:30:00 – 10:00:00"
because the aware UTC slot datetimes were formatted with ``strftime`` directly.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.communication.email_branding import format_local_dt, local_date
from iic_booking.communication.service import CommunicationService

pytestmark = pytest.mark.django_db

UTC = dt_timezone.utc
# DailySlot 45318 in production: Slot 3, 14:00–15:30 IST, stored as 08:30–10:00 UTC.
SLOT_START = datetime(2026, 10, 5, 8, 30, tzinfo=UTC)
SLOT_END = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
# 19:00 UTC is 00:30 IST on the next calendar day.
LATE_UTC = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)


@pytest.fixture
def sent(monkeypatch):
    calls = SimpleNamespace(emails=[], pushes=[])

    def _email(recipient=None, template=None, template_context=None, metadata=None, created_by=None, **kwargs):
        calls.emails.append(SimpleNamespace(recipient=recipient, template=template, ctx=dict(template_context or {})))

    def _push(recipient=None, title=None, message=None, **kwargs):
        calls.pushes.append(SimpleNamespace(recipient=recipient, title=title, message=message))

    monkeypatch.setattr(CommunicationService, "send_email", staticmethod(_email))
    monkeypatch.setattr(CommunicationService, "send_push_notification", staticmethod(_push))
    return calls


@pytest.fixture
def fixed_now(monkeypatch):
    def _set(value):
        monkeypatch.setattr(timezone, "now", lambda: value)
        return value

    return _set


def _booking_with_slot(egs_factory, start=SLOT_START, minutes=90, equipment=None, **equipment_kwargs):
    eq = equipment or egs_factory.equipment(**equipment_kwargs)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, start, slot_count=0)
    booking.total_time_minutes = minutes
    booking.save(update_fields=["total_time_minutes"])
    slot = egs_factory.slot(eq, start, minutes=minutes, status="BOOKED", booking=booking)
    return booking, slot


# --- shared helper ---------------------------------------------------------------------------------


def test_format_local_dt_converts_utc_to_ist():
    assert format_local_dt(SLOT_START) == "2026-10-05 14:00"
    assert format_local_dt(SLOT_END, "%Y-%m-%d %H:%M:%S") == "2026-10-05 15:30:00"
    assert format_local_dt(SLOT_START, "%d %b %Y, %I:%M %p") == "05 Oct 2026, 02:00 PM"


def test_format_local_dt_midnight_boundary_rolls_to_next_ist_day():
    assert format_local_dt(LATE_UTC, "%Y-%m-%d %H:%M") == "2026-10-06 00:30"
    assert local_date(LATE_UTC) == date(2026, 10, 6)
    assert LATE_UTC.date() == date(2026, 10, 5)


def test_format_local_dt_empty_and_naive_values():
    assert format_local_dt(None) == ""
    assert format_local_dt("") == ""
    assert format_local_dt(datetime(2026, 10, 5, 14, 0)) == "2026-10-05 14:00"
    assert local_date(None) is None


# --- booking reminder (the reported bug) -----------------------------------------------------------


def test_reminder_context_shows_ist_slot_times(egs_factory):
    from iic_booking.equipment.booking_reminders import build_reminder_context

    booking, _slot = _booking_with_slot(egs_factory)

    ctx = build_reminder_context(booking)

    assert ctx["start_time"] == "2026-10-05 14:00:00"
    assert ctx["end_time"] == "2026-10-05 15:30:00"
    assert "08:30" not in ctx["start_time"]
    assert ctx["total_hours"] == "1.5"


def test_send_reminder_emails_the_ist_context(egs_factory, sent):
    from iic_booking.equipment.booking_reminders import send_reminder_for_booking

    booking, _slot = _booking_with_slot(egs_factory)

    send_reminder_for_booking(booking)

    assert len(sent.emails) == 1
    email = sent.emails[0]
    assert email.template == "booking_reminder_email"
    assert email.recipient == booking.user
    assert (email.ctx["start_time"], email.ctx["end_time"]) == ("2026-10-05 14:00:00", "2026-10-05 15:30:00")


def test_reminder_slot_id_equipment_shows_ist_date_after_midnight(egs_factory):
    from iic_booking.equipment.booking_reminders import build_reminder_context

    booking, _slot = _booking_with_slot(egs_factory, start=LATE_UTC, minutes=60, weekly_view_display="SLOT_ID")

    ctx = build_reminder_context(booking)

    assert ctx["start_time"] == "2026-10-06"
    assert ctx["end_time"] == ""


def test_reminder_full_time_after_midnight(egs_factory):
    from iic_booking.equipment.booking_reminders import build_reminder_context

    booking, _slot = _booking_with_slot(egs_factory, start=LATE_UTC, minutes=60)

    ctx = build_reminder_context(booking)

    assert ctx["start_time"] == "2026-10-06 00:30:00"
    assert ctx["end_time"] == "2026-10-06 01:30:00"


# --- sample submission deadline reminder (email + push) --------------------------------------------


def test_sample_submission_deadline_reminder_uses_ist(egs_factory, sent, monkeypatch):
    from iic_booking.equipment import sample_submission_deadline_reminders as mod

    booking, _slot = _booking_with_slot(egs_factory)
    deadline = LATE_UTC - timedelta(days=1)  # 04 Oct 19:00 UTC = 05 Oct 00:30 IST
    monkeypatch.setattr(mod, "is_within_sample_submission_advance_window", lambda b: (True, deadline, 5400))

    assert mod.send_sample_submission_deadline_reminder(booking) is True

    ctx = sent.emails[0].ctx
    assert ctx["start_time"] == "2026-10-05 14:00:00"
    assert ctx["end_time"] == "2026-10-05 15:30:00"
    assert ctx["submission_deadline"] == "2026-10-05 00:30:00"
    assert "by 2026-10-05 00:30:00 " in sent.pushes[0].message


# --- booking not utilized --------------------------------------------------------------------------


def test_booking_not_utilized_slot_details_in_ist(egs_factory, sent):
    from iic_booking.equipment.booking_not_utilized_service import send_booking_not_utilized_emails

    booking, slot = _booking_with_slot(egs_factory)

    send_booking_not_utilized_emails(booking, [slot])

    assert sent.emails[0].ctx["slot_details"] == "2026-10-05 14:00-15:30"


# --- operator unavailable --------------------------------------------------------------------------


def test_operator_unavailable_email_uses_ist(egs_factory, sent, monkeypatch):
    from iic_booking.equipment import operator_unavailable as mod
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    booking, _slot = _booking_with_slot(egs_factory)
    target = SimpleNamespace(credit=lambda **kwargs: None)
    monkeypatch.setattr(WalletRepository, "get_booking_wallet_target", staticmethod(lambda u, d: (target, True)))
    monkeypatch.setattr(mod, "_student_booking_description_suffix", lambda *a, **k: "")
    monkeypatch.setattr(mod, "notify_waitlist_slots_available", lambda *a, **k: 0)

    mod.apply_operator_unavailable_booking(booking, notes="", actor=None)

    ctx = next(e.ctx for e in sent.emails if e.template == "operator_unavailable_email")
    assert (ctx["start_time"], ctx["end_time"]) == ("2026-10-05 14:00", "2026-10-05 15:30")


# --- booking completed email + sample collection deadline ------------------------------------------


def test_completion_email_context_uses_ist(egs_factory, monkeypatch):
    from iic_booking.equipment import api_views

    booking, _slot = _booking_with_slot(egs_factory)
    captured = {}

    def _render(template, context=None):
        captured.update(context)
        return {"subject": "Booking completed", "message": "", "html_message": ""}

    monkeypatch.setattr(CommunicationService, "get_template", staticmethod(lambda **kwargs: object()))
    monkeypatch.setattr(CommunicationService, "render_template", staticmethod(_render))

    api_views._send_completion_email_with_attachments(booking, [])

    assert captured["start_time"] == "2026-10-05 14:00:00"
    assert captured["end_time"] == "2026-10-05 15:30:00"


def test_sample_collection_deadline_date_is_the_ist_day(egs_factory):
    from iic_booking.equipment import api_views

    booking, _slot = _booking_with_slot(egs_factory)
    booking.equipment.sample_collect_deadline_hours = 1
    booking.completed_at = LATE_UTC - timedelta(hours=1)  # 18:00 UTC; +1h = 00:30 IST on 06 Oct

    ctx = api_views._build_sample_notice_context(booking)

    assert ctx["sample_collection_deadline_display"] == "06 October 2026"
    assert "before 06 October 2026." in ctx["sample_collection_notice"]


# --- booking event comments (shown in booking history and email notes) -----------------------------


def _reschedule_setup(egs_factory):
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.tests.factories import UserFactory

    eq = egs_factory.equipment()
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(days=5, hour=10))
    new_slot = egs_factory.slot(eq, egs_factory.future(days=6, hour=14))
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(booking=booking, owner=owner, new_slot=new_slot)


@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_reschedule_comment_uses_ist_even_for_utc_input(egs_factory, egs_quiet_side_effects, endpoint):
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    w = _reschedule_setup(egs_factory)
    actor = UserFactory(user_type=UserType.ADMIN, is_staff=True) if endpoint == "reschedule" else w.owner
    body = {
        "start_time": w.new_slot.start_datetime.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end_time": w.new_slot.end_datetime.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }

    res = egs_factory.client_for(actor).post(f"/api/bookings/{w.booking.pk}/{endpoint}/", body, format="json")

    assert res.status_code == 200, res.data
    comment = next(e["comment"] for e in egs_quiet_side_effects.events if e.get("event_type") == "RESCHEDULED")
    start_local = timezone.localtime(w.new_slot.start_datetime)
    end_local = timezone.localtime(w.new_slot.end_datetime)
    assert start_local.hour == 14
    assert comment == (
        f"Booking rescheduled to {start_local:%Y-%m-%d %H:%M} - {end_local:%Y-%m-%d %H:%M}"
    )


def test_results_available_comment_uses_ist(egs_factory, fixed_now):
    from iic_booking.equipment import api_views
    from iic_booking.equipment.models import BookingEvent

    booking, _slot = _booking_with_slot(egs_factory)
    fixed_now(LATE_UTC)

    api_views._apply_results_available_event_and_completed_status(booking)

    comment = BookingEvent.objects.filter(booking=booking).latest("event_id").comment
    assert comment == "Results are now available for this booking. Detected on 06 Oct 2026, 12:30 AM."


# --- TA nomination outcome summary (API display string) --------------------------------------------


def test_nomination_outcome_summary_uses_ist_date():
    from iic_booking.equipment import api_views
    from iic_booking.equipment.models import StudentEquipmentNominationStatus

    person = SimpleNamespace(email="p@example.com", name="Person", department=None)
    nom = SimpleNamespace(
        id=1, student=person, student_id=1, supervisor=person, supervisor_id=2, approved_by=person,
        approved_by_id=3, approved_at=LATE_UTC, status=StudentEquipmentNominationStatus.APPROVED,
        equipment=SimpleNamespace(code="EQ", name="Equipment"), equipment_id=1,
        semester=SimpleNamespace(code="2026-27", name="2026-27"), semester_id=1, remarks="",
        nominated_at=None, ta_call_id=None, resume=None, resume_submitted_at=None,
    )

    data = api_views._nomination_to_dict(nom)

    assert data["outcome_summary"].endswith(" on 06 Oct 2026")
    assert data["approved_at"] == LATE_UTC.isoformat()


# --- invoice PDF -----------------------------------------------------------------------------------


def test_invoice_pdf_date_is_the_ist_day(egs_factory):
    from pypdf import PdfReader

    from iic_booking.equipment.document_exports import build_booking_invoice_pdf
    from iic_booking.equipment.models import Booking

    booking, _slot = _booking_with_slot(egs_factory)
    Booking.objects.filter(pk=booking.pk).update(created_at=LATE_UTC)
    booking.refresh_from_db()

    pdf = build_booking_invoice_pdf(booking=booking, billing_profile=None)

    text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Date 2026-10-06" in text


# --- remote analysis setup booking list date -------------------------------------------------------


def test_analysis_setup_booking_date_is_the_ist_day(egs_factory):
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import _first_slot

    booking, _slot = _booking_with_slot(egs_factory, start=LATE_UTC, minutes=60)

    assert _first_slot(booking) == "2026-10-06"


# --- admin / string forms --------------------------------------------------------------------------


def test_daily_slot_str_uses_ist(egs_factory):
    _booking, slot = _booking_with_slot(egs_factory)

    assert str(slot).endswith("2026-10-05 (14:00 - 15:30)")


# --- notice board auto-close comment ---------------------------------------------------------------


def test_notice_auto_close_comment_uses_ist(egs_factory, fixed_now):
    from iic_booking.communication.models import Notice
    from iic_booking.communication.notice_board_service import expire_equipment_linked_notices

    eq = egs_factory.equipment()
    notice = Notice.objects.create(
        title="Unavailable",
        description="Down",
        equipment=eq,
        source=Notice.Source.EQUIPMENT_UNAVAILABLE,
        approval_status=Notice.ApprovalStatus.PENDING,
    )
    fixed_now(LATE_UTC)

    assert expire_equipment_linked_notices(equipment=eq) == 1

    notice.refresh_from_db()
    assert notice.review_comment == "Auto-closed: equipment returned to Operational at 06 Oct 2026, 12:30 AM."


# --- research copilot default slot-search day ------------------------------------------------------


def test_copilot_slot_search_defaults_to_tomorrow_in_ist(egs_factory, fixed_now, monkeypatch):
    from iic_booking.research_copilot.services import tools
    from iic_booking.research_copilot.services.v2 import slot_availability

    eq = egs_factory.equipment()
    seen = {}

    def _find(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(ok=True, rows=[], message="", error=None, slot_window_max_date=None)

    monkeypatch.setattr(slot_availability, "find_bookable_slots", _find)
    fixed_now(LATE_UTC)  # 00:30 IST on 06 Oct, still 05 Oct in UTC

    tools._search_slots(arguments={"equipment_id": eq.pk}, user=egs_factory.student())

    assert seen["start_date"] == date(2026, 10, 7)
