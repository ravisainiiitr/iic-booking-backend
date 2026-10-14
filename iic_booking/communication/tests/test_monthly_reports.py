"""Monthly usage report emails: IST month boundaries, test-data exclusion, idempotent sending, opt-out, peak window."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from rest_framework.test import APIClient

from iic_booking.communication import monthly_reports as mr
from iic_booking.communication import notification_preferences as prefs
from iic_booking.communication.models import (
    CommunicationLog,
    MonthlyReportSendLog,
    MonthlyReportSettings,
    NotificationDeliverySettings,
    NotificationPreference,
)
from iic_booking.communication.monthly_report_data import IST, ReportMonth, faculty_section, hod_section, oic_section
from iic_booking.equipment.models import Booking, BookingStatus, EquipmentManager
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models import SubWallet, Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

OCT = ReportMonth(2026, 10)
RUN_AT = datetime(2026, 11, 2, 9, 10, tzinfo=IST)  # Monday


@pytest.fixture(autouse=True)
def _portal(settings, monkeypatch):
    settings.FRONTEND_URL = "https://portal.example"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setattr("iic_booking.equipment.peak_window.current_peak_window", lambda at=None: None)


@pytest.fixture
def f():
    return _EgsFactory()


def _ist(y, m, d, h=10, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=IST)


def _book(f, owner, eq, start, *, created=None, charge="100.00", status=BookingStatus.BOOKED, slots=1):
    b = f.booking(owner, eq, start, total_charge=charge, slot_count=slots)
    Booking.objects.filter(pk=b.pk).update(created_at=created or start, status=status)
    return b


def _faculty(f, *, test=False):
    user = UserFactory(user_type=UserType.FACULTY, department=f.department, is_test_account=test, admin_approved=True)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    SubWallet.objects.get_or_create(wallet=wallet, department=f.department, defaults={"balance": Decimal("500.00")})
    return user, wallet


def _link(wallet, student):
    return WalletJoinRequest.objects.create(
        wallet=wallet, student=student, faculty=wallet.user, status=WalletJoinRequestStatus.APPROVED
    )


def _enable(**fields):
    row = MonthlyReportSettings.get_singleton()
    for k, v in {"enabled": True, **fields}.items():
        setattr(row, k, v)
    row.save()
    return row


def _admin():
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


# Months ------------------------------------------------------------------------------------------------------------


def test_report_month_uses_ist_boundaries():
    # 31 Oct 23:59 IST is still October; 1 Nov 00:30 IST (= 31 Oct 19:00 UTC) is November.
    assert ReportMonth.previous_to(_ist(2026, 10, 31, 23, 59)) == ReportMonth(2026, 9)
    assert ReportMonth.previous_to(_ist(2026, 11, 1, 0, 30)) == OCT
    assert OCT.start_at == _ist(2026, 10, 1, 0, 0) and OCT.end_at == _ist(2026, 11, 1, 0, 0)
    assert OCT.previous == ReportMonth(2026, 9) and ReportMonth(2026, 1).previous == ReportMonth(2025, 12)
    assert OCT.label == "October 2026" and OCT.key == "2026-10"
    with pytest.raises(ValueError):
        ReportMonth.parse("2026-13")


# Aggregation -------------------------------------------------------------------------------------------------------


def test_faculty_section_counts_month_in_ist_and_leaves_out_test_data(f):
    faculty, wallet = _faculty(f)
    student = f.student()
    _link(wallet, student)
    tester = UserFactory(user_type=UserType.STUDENT, is_test_account=True)
    _link(wallet, tester)
    eq = f.equipment()
    test_eq = f.equipment(visible_to_test_accounts_only=True)

    _book(f, faculty, eq, _ist(2026, 10, 5), created=_ist(2026, 10, 1, 0, 5), charge="100.00")
    _book(f, student, eq, _ist(2026, 10, 9), created=_ist(2026, 10, 31, 23, 55), charge="50.00", slots=2)
    _book(f, student, eq, _ist(2026, 11, 3), created=_ist(2026, 11, 1, 0, 5), charge="999.00")  # November
    _book(f, student, eq, _ist(2026, 9, 29), created=_ist(2026, 9, 30, 23, 50), charge="30.00")  # September
    _book(f, tester, eq, _ist(2026, 10, 6), charge="777.00")  # test user
    _book(f, student, test_eq, _ist(2026, 10, 7), charge="888.00")  # test equipment
    _book(f, student, eq, _ist(2026, 10, 8), charge="20.00", status=BookingStatus.REFUNDED)  # counted, not charged

    s = faculty_section(faculty, OCT)

    assert s["current"]["bookings"] == 3
    assert s["current"]["charged_bookings"] == 2
    assert s["current"]["spend"] == Decimal("150.00")
    assert s["current"]["hours"] == 3.0
    assert s["previous"]["spend"] == Decimal("30.00")
    assert s["own"]["spend"] == Decimal("100.00") and s["students_linked"] == 1
    assert [r["spend"] for r in s["by_equipment"]] == [Decimal("150.00")]
    assert s["top_students"][0]["bookings"] == 1
    assert s["balance_total"] == Decimal("500.00")
    assert s["has_activity"] is True
    assert faculty_section(UserFactory(user_type=UserType.FACULTY), OCT) is None


def test_oic_section_uses_slot_month_completed_revenue_and_excludes_test_bookings(f):
    oic = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    eq = f.equipment()
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    user = f.student()
    tester = UserFactory(user_type=UserType.STUDENT, is_test_account=True)
    _book(f, user, eq, _ist(2026, 10, 1, 0, 30), charge="120.00", status=BookingStatus.COMPLETED)
    _book(f, user, eq, _ist(2026, 10, 20), charge="80.00")
    _book(f, user, eq, _ist(2026, 9, 30, 23, 0), charge="60.00", status=BookingStatus.COMPLETED)  # September slot
    _book(f, tester, eq, _ist(2026, 10, 21), charge="500.00", status=BookingStatus.COMPLETED)

    s = oic_section(oic, OCT)

    assert s["totals"]["bookings"] == 2
    assert s["totals"]["revenue"] == Decimal("120.00")
    assert s["previous"]["bookings"] == 1 and s["previous"]["revenue"] == Decimal("60.00")
    row = s["table"][0]
    assert row["equipment_id"] == eq.pk and row["link"] == f"/reports?from=2026-10-01&to=2026-10-31&eq={eq.pk}"
    assert s["has_activity"] is True
    assert oic_section(UserFactory(user_type=UserType.FACULTY), OCT) is None


def test_hod_section_covers_department_equipment_and_people(f):
    hod = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    HeadOfDepartmentAssignment.objects.create(user=hod, department=f.department, effective_from=date(2026, 1, 1))
    eq = f.equipment()
    _book(f, f.student(), eq, _ist(2026, 10, 12), charge="40.00", status=BookingStatus.COMPLETED)

    s = hod_section(hod, OCT)

    dept = s["departments"][0]
    assert dept["equipment"]["totals"]["bookings"] == 1
    assert dept["user_groups"][0]["bookings"] == 1
    assert dept["people"]["current"]["bookings"] == 1
    assert s["has_activity"] is True
    assert hod.pk in mr.active_hod_user_ids()
    assert mr.candidate_ids(["hod"])["hod"] == {hod.pk}


# Sending -----------------------------------------------------------------------------------------------------------


def test_master_switch_defaults_off_and_sends_nothing(f):
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))
    assert MonthlyReportSettings.get_singleton().enabled is False

    result = mr.run_monthly_reports(OCT, now=RUN_AT)

    assert result["status"] == "disabled" and not mail.outbox
    assert not MonthlyReportSendLog.objects.exists()


def test_run_is_idempotent_skips_empty_and_carries_unsubscribe(f):
    _enable()
    active, _ = _faculty(f)
    idle, _ = _faculty(f)
    _faculty(f, test=True)
    _book(f, active, f.equipment(), _ist(2026, 10, 5), charge="100.00")

    first = mr.run_monthly_reports(OCT, now=RUN_AT, gap_seconds=0)
    second = mr.run_monthly_reports(OCT, now=RUN_AT, gap_seconds=0)

    assert first["sent"] == 1 and first["skipped_empty"] == 1
    assert second["sent"] == 0 and second["skipped_empty"] == 0
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == [active.email]
    assert "October 2026" in msg.subject
    assert msg.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg.extra_headers["List-Unsubscribe"].startswith("<https://portal.example/api/notifications/unsubscribe/")
    html = msg.alternatives[0][0]
    assert "Your research group" in html and "https://portal.example/unsubscribe?t=" in html
    statuses = dict(MonthlyReportSendLog.objects.values_list("user_id", "status"))
    assert statuses == {active.pk: "sent", idle.pk: "skipped_empty"}
    log = CommunicationLog.objects.get(recipient=active)
    assert log.metadata["notification_category"] == prefs.MONTHLY_REPORTS


def test_opted_out_user_is_skipped_and_one_click_unsubscribe_switches_reports_off(f):
    _enable()
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))
    token = prefs.unsubscribe_token(faculty, prefs.MONTHLY_REPORTS)

    resp = APIClient().post(f"/api/notifications/unsubscribe/{token}/", "List-Unsubscribe=One-Click",
                            content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 200
    assert NotificationPreference.objects.get(user=faculty, category=prefs.MONTHLY_REPORTS).email_enabled is False

    result = mr.run_monthly_reports(OCT, now=RUN_AT, gap_seconds=0)

    assert result["skipped_opted_out"] == 1 and not mail.outbox
    assert MonthlyReportSendLog.objects.get(user=faculty).status == "skipped_opted_out"


def test_quiet_hours_leave_the_report_pending(f):
    _enable()
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))
    NotificationDeliverySettings.objects.create(user=faculty, quiet_hours_enabled=True)  # 22:00–07:00
    night = datetime(2026, 11, 2, 23, 0, tzinfo=IST)

    result = mr.run_monthly_reports(OCT, now=night, gap_seconds=0)

    assert result["quiet"] == 1 and result["retry_at"] is not None and not mail.outbox
    assert not MonthlyReportSendLog.objects.exists()


@pytest.mark.parametrize(
    "at, blocked",
    [
        (datetime(2026, 11, 4, 20, 55, tzinfo=IST), True),  # Wednesday
        (datetime(2026, 11, 4, 21, 14, tzinfo=IST), True),
        (datetime(2026, 11, 4, 21, 15, tzinfo=IST), False),
        (datetime(2026, 11, 4, 20, 54, tzinfo=IST), False),
        (datetime(2026, 11, 5, 21, 0, tzinfo=IST), False),  # Thursday
    ],
)
def test_wednesday_peak_window_blocks_sending(at, blocked):
    assert (mr.blackout_until(at) is not None) is blocked


def test_run_inside_peak_window_defers_without_sending(f, monkeypatch):
    _enable()
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))

    result = mr.run_monthly_reports(OCT, now=datetime(2026, 11, 4, 21, 0, tzinfo=IST))
    assert result["status"] == "deferred" and result["retry_at"] == datetime(2026, 11, 4, 21, 15, tzinfo=IST)

    window = type("W", (), {"ends_at": datetime(2026, 11, 2, 9, 30, tzinfo=IST)})()
    monkeypatch.setattr("iic_booking.equipment.peak_window.current_peak_window", lambda at=None: window)
    assert mr.run_monthly_reports(OCT, now=RUN_AT)["status"] == "deferred"
    assert not mail.outbox and not MonthlyReportSendLog.objects.exists()


def test_disabled_audience_is_not_sent(f):
    _enable(faculty_enabled=False)
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))

    result = mr.run_monthly_reports(OCT, now=RUN_AT, gap_seconds=0)

    assert result["sent"] == 0 and not mail.outbox


def test_oic_report_attaches_excel(f):
    _enable()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    eq = f.equipment()
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    _book(f, f.student(), eq, _ist(2026, 10, 14), status=BookingStatus.COMPLETED)

    mr.run_monthly_reports(OCT, now=RUN_AT, gap_seconds=0)

    (msg,) = mail.outbox
    assert msg.attachments[0][0] == "monthly-usage-report-2026-10.xlsx"
    assert "Equipment you are Officer in Charge of" in msg.alternatives[0][0]


def test_user_strings_are_escaped_and_braces_neutralised(f):
    faculty, wallet = _faculty(f)
    student = f.student()
    student.name = "<b>{{ secret }}</b>"
    student.save()
    _link(wallet, student)
    _book(f, student, f.equipment(name="Eq <script>"), _ist(2026, 10, 5))

    html = mr.preview(faculty, OCT)["html"]

    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "{{ secret }}" not in html and "&#123;&#123; secret &#125;&#125;" in html


def test_test_send_goes_to_test_accounts_only(f):
    tester, _ = _faculty(f, test=True)
    real, _ = _faculty(f)
    _book(f, real, f.equipment(), _ist(2026, 10, 5))

    result = mr.send_test_reports(OCT, [tester.pk, real.pk])

    assert [u["id"] for u in result["users"]] == [tester.pk]
    assert len(mail.outbox) == 1 and "[TEST]" in mail.outbox[0].subject
    assert not MonthlyReportSendLog.objects.exists()
    assert MonthlyReportSettings.get_singleton().enabled is False


def test_dry_run_command_prints_counts_only(f):
    faculty, _ = _faculty(f)
    _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))
    out = StringIO()

    call_command("monthly_usage_reports", "--month", "2026-10", stdout=out)

    text = out.getvalue()
    assert '"would_send": 1' in text and '"skipped_empty": 1' in text
    assert faculty.email not in text and (faculty.name or "@@") not in text
    assert "Master switch 'Send monthly usage reports': OFF" in text
    assert not mail.outbox


def test_switch_command_status_is_read_only_and_apply_turns_on_without_sending(f):
    import json

    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    crontab = CrontabSchedule.objects.create(
        minute="10", hour="9", day_of_month="1", month_of_year="*", day_of_week="*", timezone="Asia/Kolkata"
    )
    PeriodicTask.objects.create(name="monthly", task="communication.send_monthly_usage_reports", crontab=crontab)
    row = MonthlyReportSettings.get_singleton()
    row.attach_excel, row.hod_enabled = False, False
    row.save()
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2026, 10, 5))

    out = StringIO()
    call_command("monthly_reports_switch", stdout=out)
    status = json.loads(out.getvalue())
    assert status["settings_before"]["enabled"] is False and "settings_after" not in status
    assert MonthlyReportSettings.get_singleton().enabled is False
    (beat,) = status["beat"]
    assert beat["enabled"] is True and beat["crontab"] == "10 9 1 * *"
    assert beat["next_run_at"].endswith("-01 09:10:00+05:30")

    out = StringIO()
    call_command("monthly_reports_switch", "--apply", stdout=out)
    applied = json.loads(out.getvalue())
    assert applied["verified"] is True and applied["send_log_rows_total_after"] == 0
    row = MonthlyReportSettings.get_singleton()
    assert (row.enabled, row.faculty_enabled, row.oic_enabled, row.hod_enabled, row.attach_excel) == (
        True, True, True, True, False
    )
    assert not mail.outbox and faculty.email not in out.getvalue()


# Preferences -------------------------------------------------------------------------------------------------------


def test_monthly_reports_category_is_email_only_and_shown_to_report_recipients(f):
    faculty, _ = _faculty(f)
    student = f.student()

    rows = {r["key"]: r for r in prefs.preferences_payload(faculty)["categories"]}
    assert rows[prefs.MONTHLY_REPORTS]["email_only"] is True and rows[prefs.MONTHLY_REPORTS]["email"] is True
    assert prefs.MONTHLY_REPORTS not in {r["key"] for r in prefs.preferences_payload(student)["categories"]}
    assert prefs.category_for("monthly_usage_report_email") == prefs.MONTHLY_REPORTS


# Admin API ---------------------------------------------------------------------------------------------------------


def test_preview_and_settings_are_main_admin_only_and_never_send(f):
    faculty, _ = _faculty(f)
    _book(f, faculty, f.equipment(), _ist(2025, 9, 5))
    client = APIClient()
    client.force_authenticate(faculty)
    assert client.get("/api/admin/monthly-reports/preview/", {"user": faculty.pk, "month": "2025-09"}).status_code == 403
    assert client.patch("/api/admin/monthly-reports/settings/", {"enabled": True}, format="json").status_code == 403

    client.force_authenticate(_admin())
    resp = client.get("/api/admin/monthly-reports/preview/", {"user": faculty.pk, "month": "2025-09"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["month_label"] == "September 2025" and body["would_send"] is True and "Your research group" in body["html"]
    assert client.get("/api/admin/monthly-reports/preview/", {"user": faculty.pk, "month": "2999-01"}).status_code == 400

    settings_body = client.get("/api/admin/monthly-reports/settings/").json()
    assert settings_body["enabled"] is False and settings_body["faculty_enabled"] is True
    resp = client.patch("/api/admin/monthly-reports/settings/", {"hod_enabled": False}, format="json")
    assert resp.status_code == 200 and resp.json()["hod_enabled"] is False and resp.json()["enabled"] is False
    assert client.patch("/api/admin/monthly-reports/settings/", {"enabled": "yes"}, format="json").status_code == 400

    found = client.get("/api/admin/monthly-reports/recipients/", {"q": faculty.email}).json()["results"]
    assert [r["id"] for r in found] == [faculty.pk] and found[0]["audiences"] == ["faculty"]
    assert not mail.outbox
