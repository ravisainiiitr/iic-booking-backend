"""Disruption history: soft delete (permissions, hidden everywhere, slots and bookings untouched) and restore."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.equipment.disruption_service import disruption_info_by_slot
from iic_booking.equipment.models import (
    Booking,
    DailySlot,
    DisruptionEvent,
    DisruptionEventSlot,
    DisruptionServiceReport,
    EquipmentStatus,
    EquipmentTemporaryOIC,
    SlotStatus,
    SlotStatusChangeLog,
)
from iic_booking.equipment.tests.test_disruption_log import (
    LIST_URL,
    _book,
    _bulk,
    _client,
    _department,
    _equipment,
    _event,
    _future_weekday,
    _oic_for,
    _slots,
    _user,
)
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_holidays():
    from iic_booking.equipment.models import Holiday

    Holiday.objects.all().delete()


def _delete(user, event, reason=""):
    return _client(user).post(f"{LIST_URL}{event.pk}/delete/", {"reason": reason}, format="json")


def _list_ids(user, **params):
    return {r["id"] for r in _client(user).get(LIST_URL, params).data["results"]}


def test_delete_permission_matrix():
    dept, other_dept = _department("Chem"), _department("Phys")
    mine, covering, others = (
        _equipment(internal_department=dept),
        _equipment(internal_department=other_dept),
        _equipment(internal_department=other_dept),
    )
    oic = _oic_for(mine)
    EquipmentTemporaryOIC.objects.create(
        equipment=covering, primary_oic=_user(UserType.MANAGER), temporary_oic=oic,
        resume_at=timezone.now() + timedelta(days=2),
    )
    dept_admin = _user(UserType.DEPT_ADMIN, department=dept)
    admin = _user(UserType.ADMIN)

    for role in (UserType.OPERATOR, UserType.STUDENT, UserType.FACULTY):
        assert _delete(_user(role), _event(mine)).status_code == 403
    assert _delete(oic, _event(others)).status_code == 403
    assert _delete(_oic_for(others), _event(mine)).status_code == 403
    assert _delete(dept_admin, _event(others)).status_code == 403
    assert _client(admin).post(f"{LIST_URL}999999/delete/", {}, format="json").status_code == 404

    assert _delete(oic, _event(mine)).status_code == 200
    assert _delete(oic, _event(covering)).status_code == 200  # active temporary OIC
    assert _delete(dept_admin, _event(mine)).status_code == 200
    assert _delete(admin, _event(others)).status_code == 200

    data = _client(oic).get(LIST_URL).data
    assert data["can_delete"] is True and data["can_view_deleted"] is False
    assert _client(admin).get(LIST_URL).data["can_view_deleted"] is True


def test_soft_delete_hides_from_list_summary_attention_and_export(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = _equipment()
    oic = _oic_for(eq)
    admin = _user(UserType.ADMIN)
    keep = _event(eq, reason="Kept entry")
    gone = _event(eq, reason="Wrong entry", start_at=timezone.now() - timedelta(hours=5),
                  end_at=None, scope="EQUIPMENT")
    report = DisruptionServiceReport.objects.create(
        event=gone, original_name="r.pdf", content_type="application/pdf", size_bytes=5, file="x/r.pdf"
    )

    res = _delete(oic, gone, reason="Recorded by mistake")
    assert res.status_code == 200 and res.data["was_open"] is True
    gone.refresh_from_db()
    assert gone.is_deleted and gone.deleted_by_id == oic.pk and gone.delete_reason == "Recorded by mistake"
    assert gone.edits.filter(kind="deleted").exists()
    assert DisruptionServiceReport.objects.filter(pk=report.pk).exists()  # hidden with the event, not removed

    assert _list_ids(admin) == {keep.pk}
    assert _list_ids(admin, search="Wrong") == set()
    assert _list_ids(admin, status="open") == set()
    summary = _client(admin).get(LIST_URL).data["summary"]
    assert summary["total"] == 1 and summary["open_now"] == 0
    att = _client(admin).get(f"{LIST_URL}attention/").data
    assert att["open_now"] == 0
    assert _client(admin).get(f"{LIST_URL}{gone.pk}/").status_code == 404
    assert _client(oic).get(f"{LIST_URL}{gone.pk}/service-report/{report.pk}/").status_code == 404
    assert _delete(oic, gone).status_code == 404  # already deleted

    body = _client(admin).get("/api/exports/disruption-history/", {"export_format": "csv", "show_deleted": 1})
    content = b"".join(body.streaming_content) if hasattr(body, "streaming_content") else body.content
    assert b"Kept entry" in content and b"Wrong entry" not in content


def test_delete_leaves_slots_bookings_and_change_log_untouched_and_hides_annotations():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, s10, s11, _ = _slots(eq, _future_weekday())
    booking = _book(eq, s11, _user(UserType.STUDENT))
    assert _bulk(oic, eq, status="OPERATOR_ABSENT", slot_ids=[s9.pk, s10.pk], disruption_reason="Leave").status_code == 200
    event = DisruptionEvent.objects.get(equipment=eq)
    assert set(disruption_info_by_slot(eq, [s9.pk, s10.pk])) == {s9.pk, s10.pk}

    statuses = dict(DailySlot.objects.filter(slot_master__equipment=eq).values_list("id", "status"))
    booking_state = Booking.objects.filter(pk=booking.pk).values("status", "total_charge").get()
    logs = list(SlotStatusChangeLog.objects.filter(equipment=eq).values_list("id", "new_status", "slot_count"))

    assert _delete(oic, event, reason="Duplicate").status_code == 200

    assert dict(DailySlot.objects.filter(slot_master__equipment=eq).values_list("id", "status")) == statuses
    assert Booking.objects.filter(pk=booking.pk).values("status", "total_charge").get() == booking_state
    assert list(SlotStatusChangeLog.objects.filter(equipment=eq).values_list("id", "new_status", "slot_count")) == logs
    assert DisruptionEventSlot.objects.filter(event=event).count() == 2
    assert disruption_info_by_slot(eq, [s9.pk, s10.pk]) == {}


def test_resume_and_remark_after_delete_do_not_crash_or_reuse_deleted_event():
    eq = _equipment()
    oic = _oic_for(eq)
    s9, s10, s11, _ = _slots(eq, _future_weekday())
    _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s9.pk, s10.pk])
    deleted = DisruptionEvent.objects.get(equipment=eq)
    assert _delete(oic, deleted).status_code == 200

    # Marking the next slot does not extend the deleted event: a fresh one is recorded.
    res = _bulk(oic, eq, status="UNDER_MAINTENANCE", slot_ids=[s11.pk])
    assert res.status_code == 200, res.data
    fresh = DisruptionEvent.objects.get(equipment=eq, is_deleted=False)
    assert fresh.pk != deleted.pk and res.data["disruption_events"]["opened"] == [fresh.pk]

    res = _bulk(oic, eq, status="AVAILABLE", slot_ids=[s9.pk, s10.pk, s11.pk], resolution_action="Fixed")
    assert res.status_code == 200, res.data
    assert res.data["disruption_events"]["closed"] == [fresh.pk]
    assert deleted.pk not in res.data["disruption_events"]["resumed"]
    deleted.refresh_from_db()
    assert deleted.is_deleted and deleted.action_taken == ""
    assert set(DailySlot.objects.filter(pk__in=[s9.pk, s10.pk, s11.pk]).values_list("status", flat=True)) == {
        SlotStatus.AVAILABLE
    }


def test_equipment_back_operational_after_deleting_its_open_event():
    eq = _equipment()
    oic = _oic_for(eq)
    with patch("iic_booking.equipment.maintenance_policy.send_mail"):
        assert _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "REPAIR"}, format="json").status_code == 200
    event = DisruptionEvent.objects.get(equipment=eq)
    assert _delete(oic, event).status_code == 200

    res = _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "ACTIVE"}, format="json")
    assert res.status_code == 200, res.data
    assert res.data.get("disruption_events", {}).get("closed", []) == []
    eq.refresh_from_db()
    assert eq.status == EquipmentStatus.ACTIVE

    eq.status = EquipmentStatus.REPAIR
    eq.save()
    assert DisruptionEvent.objects.filter(equipment=eq, is_deleted=False, scope="EQUIPMENT").count() == 1


def test_deleted_other_reasons_entry_leaves_report_disruption_hours():
    from iic_booking.equipment.reports import get_equipment_report_data

    eq = _equipment()
    admin = _user(UserType.ADMIN)
    day = _future_weekday()
    s9, *_ = _slots(eq, day)
    _bulk(admin, eq, status="BLOCKED", slot_ids=[s9.pk], blocked_label="Power cut")

    def disruption_hours():
        # Reports count slot time that has passed: read it the day after.
        with patch("iic_booking.equipment.utilization._now", return_value=s9.end_datetime + timedelta(days=1)):
            return get_equipment_report_data(day.isoformat(), day.isoformat(), [eq.pk])["summary"]["disruption_hours"]

    assert disruption_hours() == pytest.approx(1.0)
    assert _delete(admin, DisruptionEvent.objects.get(equipment=eq)).status_code == 200
    assert disruption_hours() == pytest.approx(0.0)


def test_main_admin_can_list_and_restore_deleted_entries():
    eq = _equipment()
    oic = _oic_for(eq)
    admin = _user(UserType.ADMIN)
    event = _event(eq, reason="Restore me")
    _delete(oic, event, reason="Oops")

    data = _client(admin).get(LIST_URL, {"show_deleted": 1}).data
    assert data["show_deleted"] is True
    row = next(r for r in data["results"] if r["id"] == event.pk)
    assert row["is_deleted"] is True and row["delete_reason"] == "Oops" and row["deleted_at"]
    assert _list_ids(oic, show_deleted=1) == set()  # ignored for other roles

    url = f"{LIST_URL}{event.pk}/restore/"
    assert _client(oic).post(url, {}, format="json").status_code == 403
    assert _client(_user(UserType.DEPT_ADMIN, department=eq.internal_department)).post(url, {}).status_code == 403
    res = _client(admin).post(url, {}, format="json")
    assert res.status_code == 200
    event.refresh_from_db()
    assert not event.is_deleted and event.edits.filter(kind="restored").exists()
    assert _list_ids(admin) == {event.pk}
    assert _client(admin).post(url, {}, format="json").status_code == 404
