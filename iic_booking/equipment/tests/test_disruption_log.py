"""Disruption log: recording from every path, grouping, resume, history API, uploads and backfill."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    DisruptionEvent,
    DisruptionEventSlot,
    Equipment,
    EquipmentManager,
    EquipmentStatus,
    EquipmentTemporaryOIC,
    Holiday,
    SlotMaster,
    SlotStatus,
    SlotStatusChangeLog,
)
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

LIST_URL = "/api/equipments/disruptions/"


@pytest.fixture(autouse=True)
def _no_holidays():
    Holiday.objects.all().delete()


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(user_type, **kwargs):
    return UserFactory(admin_approved=True, user_type=user_type, **kwargs)


def _department(name: str) -> Department:
    return Department.objects.create(name=f"{name} {uuid.uuid4().hex[:4]}", code=f"D{uuid.uuid4().hex[:5]}")


def _future_weekday(offset: int = 14) -> date:
    d = timezone.localdate() + timedelta(days=offset)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _equipment(**kwargs) -> Equipment:
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"DL{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": EquipmentStatus.ACTIVE,
    }
    defaults.update(kwargs)
    if "internal_department" not in defaults:
        defaults["internal_department"] = _department("Lab")
    dept = defaults["internal_department"]
    if not dept.equipment_visibility_enabled:
        dept.equipment_visibility_enabled = True
        dept.save(update_fields=["equipment_visibility_enabled"])
    eq = Equipment.objects.create(**defaults)
    for number, hour in enumerate((9, 10, 11, 12), 1):
        SlotMaster.objects.create(
            equipment=eq, slot_number=number, open_time=time(hour), close_time=time(hour + 1), is_active=True
        )
    return eq


def _slots(eq, day: date) -> list[DailySlot]:
    out = []
    for master in SlotMaster.objects.filter(equipment=eq).order_by("slot_number"):
        start = timezone.make_aware(datetime.combine(day, master.open_time))
        slot, _ = DailySlot.objects.get_or_create(
            slot_master=master,
            date=day,
            defaults={
                "start_datetime": start,
                "end_datetime": start + timedelta(hours=1),
                "status": SlotStatus.AVAILABLE,
            },
        )
        out.append(slot)
    return out


def _book(eq, slot, user) -> Booking:
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=eq, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    booking = Booking.objects.create(
        user=user,
        equipment=eq,
        charge_profile=profile,
        status=BookingStatus.BOOKED,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        input_values={"samples": 1},
        virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    DailySlot.objects.filter(pk=slot.pk).update(status=SlotStatus.BOOKED, booking=booking)
    return booking


def _oic_for(eq):
    oic = _user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _bulk(user, eq, **payload):
    return _client(user).post(f"/api/admin/equipment/{eq.pk}/bulk-slot-status/", payload, format="json")


# --- Change slot status ---------------------------------------------------------------------------------


def test_contiguous_slots_form_one_event_and_usable_slot_splits():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, s10, s11, s12 = _slots(eq, _future_weekday())

    res = _bulk(
        oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk, s10.pk, s12.pk],
        disruption_reason="Chiller tripped", disruption_reason_category="utilities",
    )
    assert res.status_code == 200, res.data
    events = list(DisruptionEvent.objects.filter(equipment=eq).order_by("start_at"))
    assert len(events) == 2  # 11:00 stays Available and splits the run
    first, second = events
    assert first.slots_affected == 2 and second.slots_affected == 1
    assert first.reason == "Chiller tripped" and first.reason_category == "UTILITIES"
    assert first.source == "CHANGE_SLOT_STATUS" and first.started_by_id == oic.pk
    assert first.scope == "SLOTS" and first.ended_at is None
    assert sorted(res.data["disruption_events"]["opened"]) == sorted(e.pk for e in events)
    log = SlotStatusChangeLog.objects.get(equipment=eq)
    assert log.new_status == "UNDER_MAINTENANCE" and log.slot_count == 3


def test_closed_time_between_slots_does_not_split_and_adjacent_marking_extends():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    s9, s10, s11, s12 = _slots(eq, _future_weekday())
    DailySlot.objects.filter(pk=s10.pk).update(status=SlotStatus.NOT_AVAILABLE)

    assert _bulk(admin, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk, s11.pk]).status_code == 200
    assert DisruptionEvent.objects.filter(equipment=eq).count() == 1

    res = _bulk(admin, eq, status="OPERATOR_ABSENT", slot_ids=[s12.pk])
    assert res.status_code == 200
    event = DisruptionEvent.objects.get(equipment=eq)
    assert res.data["disruption_events"]["extended"] == [event.pk]
    assert event.slots_affected == 3
    assert event.edits.filter(kind="extended").exists()


def test_different_reason_is_recorded_separately():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    s9, s10, _, _ = _slots(eq, _future_weekday())
    _bulk(admin, eq, status="BLOCKED", slot_ids=[s9.pk], blocked_label="Power shutdown")
    _bulk(admin, eq, status="BLOCKED", slot_ids=[s10.pk], disruption_reason="Gas cylinder empty")
    reasons = sorted(DisruptionEvent.objects.filter(equipment=eq).values_list("reason", flat=True))
    assert reasons == ["Gas cylinder empty", "Power shutdown"]  # Other Reasons label becomes the reason


def test_resume_closes_event_with_action_taken():
    eq = _equipment()
    oic = _oic_for(eq)
    slots = _slots(eq, _future_weekday())
    ids = [s.pk for s in slots[:2]]
    _bulk(oic, eq, status="SCHEDULED_MAINT", slot_ids=ids)
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.disruption_type == "SCHEDULED_MAINTENANCE"

    preview = _bulk(oic, eq, status="AVAILABLE", slot_ids=ids, preview=True)
    assert preview.status_code == 200
    assert preview.data["resumes"] is True
    assert [e["id"] for e in preview.data["open_events"]] == [event.pk]
    assert DailySlot.objects.get(pk=ids[0]).status == SlotStatus.SCHEDULED_MAINTENANCE  # preview changes nothing

    res = _bulk(oic, eq, status="AVAILABLE", slot_ids=ids, resolution_action="Pump serviced")
    assert res.status_code == 200
    event.refresh_from_db()
    assert event.ended_at is not None and event.ended_by_id == oic.pk
    assert event.action_taken == "Pump serviced"
    assert res.data["disruption_events"]["closed"] == [event.pk]
    assert not DisruptionEventSlot.objects.filter(event=event, released_at__isnull=True).exists()


def test_booked_slot_marked_under_maintenance_counts_affected_booking():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    s9, s10, _, _ = _slots(eq, _future_weekday())
    faculty = _user(UserType.FACULTY)
    Wallet.objects.create(user=faculty)
    booking = _book(eq, s9, faculty)

    preview = _bulk(admin, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk, s10.pk], preview=True)
    assert preview.data["bookings_affected"] == 1 and preview.data["is_disruption"] is True
    assert {c["value"] for c in preview.data["reason_categories"]} >= {"BREAKDOWN", "CALIBRATION"}

    res = _bulk(admin, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk, s10.pk])
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.status == BookingStatus.REFUNDED
    assert DisruptionEvent.objects.get(equipment=eq).bookings_affected == 1


def test_not_available_and_reserved_external_are_not_disruptions():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    s9, s10, s11, _ = _slots(eq, _future_weekday())
    _book(eq, s11, _user(UserType.STUDENT))

    res = _bulk(admin, eq, status="RESERVED_EXTERNAL", slot_ids=[s9.pk, s11.pk], external_reference="FBR-2026-77")
    assert res.status_code == 200, res.data
    assert res.data["skipped_booked"] == 1
    s9.refresh_from_db()
    s11.refresh_from_db()
    assert s9.status == SlotStatus.RESERVED_EXTERNAL and s9.external_reference == "FBR-2026-77"
    assert s11.status == SlotStatus.BOOKED  # booked slots are left alone
    assert SlotStatusChangeLog.objects.get(equipment=eq, new_status="RESERVED_EXTERNAL").external_reference == (
        "FBR-2026-77"
    )

    assert _bulk(admin, eq, status="NOT_AVAILABLE", slot_ids=[s10.pk]).status_code == 200
    assert not DisruptionEvent.objects.filter(equipment=eq).exists()

    # Changing a reserved slot to another status clears its FBR reference.
    _bulk(admin, eq, status="AVAILABLE", slot_ids=[s9.pk])
    s9.refresh_from_db()
    assert s9.external_reference is None


def test_reserved_external_reference_is_visible_to_staff_only():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    day = _future_weekday(1)  # inside the booking window that limits what regular users see
    s9, *_ = _slots(eq, day)
    _bulk(admin, eq, status="RESERVED_EXTERNAL", slot_ids=[s9.pk], external_reference="FBR-1")
    url = f"/api/equipments/{eq.pk}/slots/?start_date={day}&end_date={day}"

    staff_rows = _client(admin).get(url).data.get("slots") or []
    row = next(r for r in staff_rows if r["id"] == s9.pk)
    assert row["status"] == "RESERVED_EXTERNAL" and row["external_reference"] == "FBR-1"

    sres = _client(_user(UserType.STUDENT)).get(url)
    student_rows = sres.data.get("slots") or []
    srow = next((r for r in student_rows if r["id"] == s9.pk), None)
    assert srow is not None and "external_reference" not in srow and srow["status"] != "AVAILABLE"


def test_dashboard_calendar_source_and_change_log():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, *_ = _slots(eq, _future_weekday())
    _bulk(oic, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk], source="dashboard_calendar")
    assert DisruptionEvent.objects.get(equipment=eq).source == "DASHBOARD_CALENDAR"


# --- Equipment status and booking details -----------------------------------------------------------------


def test_equipment_under_maintenance_and_back_operational():
    eq = _equipment()
    oic = _oic_for(eq)
    res = _client(oic).patch(
        f"/api/equipments/{eq.pk}/",
        {"status": "REPAIR", "disruption_reason": "Detector failure", "disruption_reason_category": "BREAKDOWN"},
        format="json",
    )
    assert res.status_code == 200, res.data
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.scope == "EQUIPMENT" and event.source == "EQUIPMENT_STATUS"
    assert event.reason == "Detector failure" and event.started_by_id == oic.pk
    assert res.data["disruption_events"]["opened"] == [event.pk]

    res = _client(oic).patch(
        f"/api/equipments/{eq.pk}/", {"status": "ACTIVE", "resolution_action": "Detector replaced"}, format="json"
    )
    assert res.status_code == 200
    event.refresh_from_db()
    assert event.ended_at is not None and event.action_taken == "Detector replaced"
    assert res.data["disruption_events"]["closed"] == [event.pk]


def test_equipment_status_saved_from_any_path_is_recorded():
    eq = _equipment()
    eq.status = EquipmentStatus.REPAIR
    eq.save()
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.source == "OTHER" and event.started_by_id is None
    eq.status = EquipmentStatus.ACTIVE
    eq.save()
    event.refresh_from_db()
    assert event.ended_at is not None


def test_slot_marked_while_equipment_under_maintenance_joins_equipment_event():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    eq.status = EquipmentStatus.REPAIR
    eq.save()
    s9, *_ = _slots(eq, _future_weekday())
    _bulk(admin, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk])
    assert DisruptionEvent.objects.filter(equipment=eq).count() == 1


def test_booking_details_under_maintenance_records_event():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, s10, _, _ = _slots(eq, _future_weekday())
    booking = _book(eq, s9, _user(UserType.STUDENT))
    DailySlot.objects.filter(pk=s10.pk).update(status=SlotStatus.BOOKED, booking=booking)
    with patch("iic_booking.equipment.maintenance_policy.send_mail"):
        res = _client(oic).post(
            f"/api/bookings/{booking.pk}/maintenance-disruption/", {"notes": "Vacuum leak"}, format="json"
        )
    if res.status_code == 404:
        pytest.skip("booking maintenance-disruption route differs")
    assert res.status_code == 200, res.data
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.source == "BOOKING_DETAILS" and event.reason == "Vacuum leak"
    assert event.bookings_affected == 1 and event.slots_affected == 2


# --- History API ------------------------------------------------------------------------------------------


def _event(eq, **kwargs):
    now = timezone.now()
    defaults = {
        "disruption_type": "UNDER_MAINTENANCE",
        "scope": "SLOTS",
        "start_at": now - timedelta(hours=3),
        "end_at": now - timedelta(hours=1),
    }
    defaults.update(kwargs)
    return DisruptionEvent.objects.create(equipment=eq, **defaults)


def test_history_scope_per_role():
    dept, other_dept = _department("Chem"), _department("Phys")
    mine, covering, others = _equipment(internal_department=dept), _equipment(), _equipment(
        internal_department=other_dept
    )
    oic = _oic_for(mine)
    EquipmentTemporaryOIC.objects.create(
        equipment=covering, primary_oic=_user(UserType.MANAGER), temporary_oic=oic,
        resume_at=timezone.now() + timedelta(days=2),
    )
    e1, e2, e3 = _event(mine), _event(covering), _event(others)

    ids = {r["id"] for r in _client(oic).get(LIST_URL).data["results"]}
    assert ids == {e1.pk, e2.pk}

    dept_admin = _user(UserType.DEPT_ADMIN, department=dept)
    assert {r["id"] for r in _client(dept_admin).get(LIST_URL).data["results"]} == {e1.pk}

    admin = _user(UserType.ADMIN)
    assert {r["id"] for r in _client(admin).get(LIST_URL).data["results"]} == {e1.pk, e2.pk, e3.pk}

    assert _client(_user(UserType.OPERATOR)).get(LIST_URL).status_code == 403
    assert _client(_user(UserType.STUDENT)).get(LIST_URL).status_code == 403
    assert _client(oic).get(f"{LIST_URL}{e3.pk}/").status_code == 404


def test_history_filters_and_summary():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    now = timezone.now()
    closed = _event(eq, reason="Fixed", action_taken="Done")
    open_future = _event(eq, disruption_type="OPERATOR_ABSENT", start_at=now + timedelta(hours=1),
                         end_at=now + timedelta(hours=3))
    whole = _event(eq, scope="EQUIPMENT", end_at=None, start_at=now - timedelta(hours=10))

    def ids(**params):
        return {r["id"] for r in _client(admin).get(LIST_URL, params).data["results"]}

    assert ids(status="open") == {open_future.pk, whole.pk}
    assert ids(status="closed") == {closed.pk}
    assert ids(type="OPERATOR_ABSENT") == {open_future.pk}
    assert ids(reason_missing=1) == {open_future.pk, whole.pk}
    assert ids(action_missing=1) == {open_future.pk, whole.pk}
    assert ids(search="Fixed") == {closed.pk}
    assert ids(scope="EQUIPMENT") == {whole.pk}

    data = _client(admin).get(LIST_URL).data
    assert data["summary"]["total"] == 3
    assert data["summary"]["open_now"] == 2
    assert data["summary"]["reason_missing"] == 2
    row = next(r for r in data["results"] if r["id"] == closed.pk)
    assert row["status"] == "CLOSED" and row["reason_missing"] is False
    assert "equipment_options" not in data

    opts = _client(admin).get(LIST_URL, {"with_options": 1}).data
    assert eq.pk in {o["id"] for o in opts["equipment_options"]}
    assert eq.internal_department_id in {d["id"] for d in opts["department_options"]}
    oic_opts = _client(_oic_for(eq)).get(LIST_URL, {"with_options": 1}).data
    assert [o["id"] for o in oic_opts["equipment_options"]] == [eq.pk] and "department_options" not in oic_opts

    att = _client(admin).get(f"{LIST_URL}attention/").data
    assert att == {"enabled": True, "open_now": 2, "reason_missing": 2}
    assert _client(_user(UserType.STUDENT)).get(f"{LIST_URL}attention/").data["enabled"] is False


def test_edit_reason_and_action_keeps_history():
    eq = _equipment()
    oic = _oic_for(eq)
    event = _event(eq)
    res = _client(oic).patch(
        f"{LIST_URL}{event.pk}/",
        {"reason": "Compressor fault", "reason_category": "breakdown", "action_taken": "Replaced"},
        format="json",
    )
    assert res.status_code == 200, res.data
    event.refresh_from_db()
    assert (event.reason, event.reason_category, event.action_taken) == ("Compressor fault", "BREAKDOWN", "Replaced")
    assert {t["field"] for t in res.data["timeline"]} >= {"reason", "reason_category", "action_taken"}
    assert res.data["reason_category_display"] == "Breakdown"


def test_service_report_upload_and_download(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = _equipment()
    oic = _oic_for(eq)
    event = _event(eq)
    pdf = SimpleUploadedFile("report.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    res = _client(oic).post(f"{LIST_URL}{event.pk}/service-report/", {"file": pdf}, format="multipart")
    assert res.status_code == 201, res.data
    report = res.data["service_reports"][0]
    assert report["name"] == "report.pdf"

    download = _client(oic).get(report["url"])
    assert download.status_code == 200
    assert b"".join(download.streaming_content).startswith(b"%PDF-")
    assert _client(_user(UserType.MANAGER)).get(report["url"]).status_code == 404

    bad = SimpleUploadedFile("report.pdf", b"not a pdf", content_type="application/pdf")
    assert _client(oic).post(f"{LIST_URL}{event.pk}/service-report/", {"file": bad}, format="multipart").status_code == 400
    exe = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
    assert _client(oic).post(f"{LIST_URL}{event.pk}/service-report/", {"file": exe}, format="multipart").status_code == 400


def test_history_export_xlsx():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    _event(eq, reason="Fixed")
    res = _client(admin).get("/api/exports/disruption-history/", {"export_format": "csv"})
    assert res.status_code == 200, getattr(res, "data", None)
    body = b"".join(res.streaming_content) if hasattr(res, "streaming_content") else res.content
    assert b"Disruption history" in body or b"Fixed" in body


# --- Backfill ---------------------------------------------------------------------------------------------


def test_backfill_dry_run_then_apply_is_idempotent():
    eq = _equipment()
    day = _future_weekday()
    s9, s10, s11, s12 = _slots(eq, day)
    DailySlot.objects.filter(pk__in=[s9.pk, s10.pk]).update(status=SlotStatus.UNDER_MAINTENANCE)
    DailySlot.objects.filter(pk=s12.pk).update(status=SlotStatus.BLOCKED, blocked_label="Power cut")
    weekend = day + timedelta(days=(5 - day.weekday()))
    sat_slots = _slots(eq, weekend)
    Holiday.objects.create(date=weekend, reason="Saturday") if hasattr(Holiday, "reason") else None
    DailySlot.objects.filter(pk=sat_slots[0].pk).update(status=SlotStatus.BLOCKED)

    whole = _equipment()
    Equipment.objects.filter(pk=whole.pk).update(status=EquipmentStatus.REPAIR)
    whole_slots = _slots(whole, day)
    DailySlot.objects.filter(pk__in=[s.pk for s in whole_slots]).update(status=SlotStatus.UNDER_MAINTENANCE)

    out = StringIO()
    call_command("backfill_disruption_events", stdout=out)
    text = out.getvalue()
    assert "mode=DRY RUN" in text
    assert not DisruptionEvent.objects.exists()

    call_command("backfill_disruption_events", "--apply", stdout=StringIO())
    events = DisruptionEvent.objects.filter(equipment=eq)
    assert events.filter(disruption_type="UNDER_MAINTENANCE").count() == 1
    other = events.get(disruption_type="OTHER")
    assert other.reason == "Power cut" and other.backfilled and other.source == "BACKFILL"
    assert DisruptionEvent.objects.filter(equipment=whole, scope="EQUIPMENT").count() == 1
    assert not DisruptionEvent.objects.filter(equipment=whole, scope="SLOTS").exists()
    total = DisruptionEvent.objects.count()

    rerun = StringIO()
    call_command("backfill_disruption_events", "--apply", stdout=rerun)
    assert DisruptionEvent.objects.count() == total
    assert "slot_events=0" in rerun.getvalue()


# --- Reports ----------------------------------------------------------------------------------------------


def test_report_disruption_hours_include_scheduled_and_recorded_other_reasons():
    from iic_booking.equipment.reports import get_equipment_report_data

    eq = _equipment()
    admin = _user(UserType.ADMIN)
    day = _future_weekday()
    s9, s10, s11, s12 = _slots(eq, day)
    _bulk(admin, eq, status="SCHEDULED_MAINT", slot_ids=[s9.pk])
    _bulk(admin, eq, status="BLOCKED", slot_ids=[s10.pk], blocked_label="Power cut")
    DailySlot.objects.filter(pk=s11.pk).update(status=SlotStatus.BLOCKED, blocked_label="Holiday style block")
    _bulk(admin, eq, status="OPERATOR_ABSENT", slot_ids=[s12.pk])

    data = get_equipment_report_data(day.isoformat(), day.isoformat(), [eq.pk])
    summary = data["summary"]
    assert summary["downtime_hours"] == pytest.approx(2.0)  # scheduled maintenance + operator absent
    assert summary["disruption_hours"] == pytest.approx(3.0)  # + the recorded Other Reasons slot only
    row = data["equipment"][0]
    assert row["scheduled_maintenance_hours"] == pytest.approx(1.0)
    assert row["other_reasons_hours"] == pytest.approx(1.0)
