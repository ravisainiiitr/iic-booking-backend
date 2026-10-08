"""Repeat block (recurring slot block rules) on the Change slot status page."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    EquipmentStatus,
    EquipmentTemporaryOIC,
    Holiday,
    RecurringSlotBlockRule,
    RecurringSlotBlockRuleSlot,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

IST = ZoneInfo("Asia/Kolkata")
# Monday 7 Jan 2030, 09:00 IST.
NOW = datetime(2030, 1, 7, 9, 0, tzinfo=IST)
MON = date(2030, 1, 7)
MONDAY, TUESDAY, THURSDAY, SATURDAY = 0, 1, 3, 5


@pytest.fixture(autouse=True)
def frozen_now():
    Holiday.objects.all().delete()
    with patch("django.utils.timezone.now", return_value=NOW.astimezone(ZoneInfo("UTC"))):
        yield


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _user(user_type, **kwargs):
    return UserFactory(admin_approved=True, user_type=user_type, **kwargs)


def _equipment(**kwargs) -> Equipment:
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"RB{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    eq = Equipment.objects.create(**defaults)
    for number, (start, end) in enumerate([(time(0, 30), time(1, 30)), (time(10), time(11)), (time(14), time(15))], 1):
        SlotMaster.objects.create(equipment=eq, slot_number=number, open_time=start, close_time=end, is_active=True)
    return eq


def _generate(eq, start: date, end: date):
    SlotGenerator.generate_slots_for_week(eq, start, end, allow_holiday=True)


def _slot(eq, day: date, hhmm: str) -> DailySlot:
    return next(
        s
        for s in DailySlot.objects.filter(slot_master__equipment=eq, date=day)
        if timezone.localtime(s.start_datetime).strftime("%H:%M") == hhmm
    )


def _book(eq, slot: DailySlot, user) -> Booking:
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


def _url(eq, suffix: str = "") -> str:
    return f"/api/admin/equipment/{eq.pk}/slot-block-rules/{suffix}"


def _payload(**overrides):
    body = {
        "weekdays": [MONDAY, THURSDAY],
        "slot_times": ["10:00", "14:00"],
        "start_date": MON.isoformat(),
        "end_date": (MON + timedelta(days=13)).isoformat(),
        "label": "Calibration",
    }
    body.update(overrides)
    return body


def _create(user, eq, **overrides):
    res = _client(user).post(_url(eq), _payload(**overrides), format="json")
    assert res.status_code == 201, res.data
    return res.data


@pytest.fixture
def admin():
    return _user(UserType.ADMIN)


@pytest.fixture
def eq():
    equipment = _equipment()
    _generate(equipment, MON, MON + timedelta(days=13))
    return equipment


# --- Permissions --------------------------------------------------------------------------------


def test_permission_matrix(eq):
    other = _equipment()
    oic = _user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    temp_oic = _user(UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp_oic, resume_at=timezone.now() + timedelta(days=3)
    )
    expired_temp = _user(UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=expired_temp, resume_at=timezone.now() - timedelta(days=1)
    )
    operator = _user(UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    dept_admin = _user(UserType.DEPT_ADMIN)
    student = _user(UserType.STUDENT)
    admin = _user(UserType.ADMIN)

    for user in (admin, oic, temp_oic):
        assert _client(user).get(_url(eq)).status_code == 200
        assert _client(user).post(_url(eq, "preview/"), _payload(), format="json").status_code == 200

    assert _client(admin).get(_url(other)).status_code == 200
    for user in (operator, dept_admin, student, expired_temp):
        assert _client(user).get(_url(eq)).status_code == 403
        assert _client(user).post(_url(eq, "preview/"), _payload(), format="json").status_code == 403
        assert _client(user).post(_url(eq), _payload(), format="json").status_code == 403
    assert _client(oic).get(_url(other)).status_code == 403
    assert _client(oic).post(_url(other), _payload(), format="json").status_code == 403
    assert _client().get(_url(eq)).status_code in (401, 403)
    assert RecurringSlotBlockRule.objects.count() == 0

    rule = _create(oic, eq)["rule"]
    assert _client(operator).post(_url(eq, f"{rule['id']}/remove/"), {}, format="json").status_code == 403
    assert _client(student).get(_url(eq, f"{rule['id']}/")).status_code == 403
    assert _client(admin).get(f"/api/admin/equipment/{other.pk}/slot-block-rules/{rule['id']}/").status_code == 404
    assert _client(admin).get("/api/admin/equipment/999999/slot-block-rules/").status_code == 404


def test_list_returns_equipment_slot_times(admin, eq):
    SlotMaster.objects.create(equipment=eq, slot_number=9, open_time=time(16), close_time=time(17), is_active=False)
    res = _client(admin).get(_url(eq))
    assert res.status_code == 200
    assert [row["time"] for row in res.data["slot_times"]] == ["00:30", "10:00", "14:00"]
    assert res.data["rules"] == []


# --- Validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"weekdays": []}, "weekdays"),
        ({"weekdays": [7]}, "weekdays"),
        ({"slot_times": []}, "slot_times"),
        ({"slot_times": ["12:00"]}, "slot_times"),
        ({"start_date": (MON - timedelta(days=1)).isoformat()}, "start_date"),
        ({"end_date": (MON - timedelta(days=1)).isoformat(), "start_date": MON.isoformat()}, "end_date"),
        ({"end_date": (MON + timedelta(days=500)).isoformat()}, "end_date"),
        ({"end_date": ""}, "end_date"),
        ({"label": "x" * 256}, "label"),
    ],
)
def test_invalid_input_is_rejected(admin, eq, overrides, field):
    res = _client(admin).post(_url(eq, "preview/"), _payload(**overrides), format="json")
    assert res.status_code == 400
    assert res.data["field"] == field
    assert res.data["detail"]
    assert _client(admin).post(_url(eq), _payload(**overrides), format="json").status_code == 400
    assert RecurringSlotBlockRule.objects.count() == 0


def test_start_date_defaults_to_today(admin, eq):
    body = _payload()
    body.pop("start_date")
    res = _client(admin).post(_url(eq), body, format="json")
    assert res.status_code == 201, res.data
    assert res.data["rule"]["start_date"] == MON.isoformat()


# --- Matching -----------------------------------------------------------------------------------


def test_matches_local_weekday_and_time_in_ist(admin, eq):
    # 00:30 IST on Monday is 19:00 UTC on Sunday: matching must use the local date and time.
    early_mon = _slot(eq, MON + timedelta(days=7), "00:30")
    assert early_mon.start_datetime.astimezone(ZoneInfo("UTC")).date() == MON + timedelta(days=6)

    data = _create(admin, eq, weekdays=[MONDAY], slot_times=["00:30"])
    assert data["result"]["blocked_count"] == 1
    early_mon.refresh_from_db()
    assert early_mon.status == SlotStatus.BLOCKED
    assert early_mon.blocked_label == "Calibration"

    # Today's 00:30 has already started; Tuesday and other times are not covered.
    assert _slot(eq, MON, "00:30").status == SlotStatus.AVAILABLE
    assert _slot(eq, MON + timedelta(days=8), "00:30").status == SlotStatus.AVAILABLE
    assert _slot(eq, MON + timedelta(days=7), "10:00").status == SlotStatus.AVAILABLE


def test_create_blocks_matching_available_slots_only(admin, eq):
    data = _create(admin, eq)
    result = data["result"]
    blocked = DailySlot.objects.filter(slot_master__equipment=eq, status=SlotStatus.BLOCKED)
    got = sorted((s.date, timezone.localtime(s.start_datetime).strftime("%H:%M")) for s in blocked)
    expected = sorted(
        (d, t)
        for d in (MON, MON + timedelta(days=3), MON + timedelta(days=7), MON + timedelta(days=10))
        for t in ("10:00", "14:00")
    )
    assert got == expected
    assert result["blocked_count"] == 8
    assert {s.blocked_label for s in blocked} == {"Calibration"}
    rule = RecurringSlotBlockRule.objects.get(pk=data["rule"]["id"])
    assert rule.created_by_id == admin.pk
    assert rule.weekdays == [MONDAY, THURSDAY]
    assert rule.slot_times == ["10:00", "14:00"]
    assert set(rule.slot_links.values_list("daily_slot_id", flat=True)) == {s.pk for s in blocked}
    assert data["rule"]["blocked_now_count"] == 8
    assert data["rule"]["removal_preview"]["will_unblock_count"] == 8


def test_empty_label_stores_no_blocked_label(admin, eq):
    _create(admin, eq, label="   ", weekdays=[MONDAY], slot_times=["14:00"])
    assert _slot(eq, MON, "14:00").blocked_label is None


def test_started_slots_are_never_touched(admin, eq):
    with patch("django.utils.timezone.now", return_value=datetime(2030, 1, 7, 12, 0, tzinfo=IST)):
        data = _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00", "14:00"])
    assert _slot(eq, MON, "10:00").status == SlotStatus.AVAILABLE
    assert _slot(eq, MON, "14:00").status == SlotStatus.BLOCKED
    assert data["result"]["blocked_count"] == 3
    assert not RecurringSlotBlockRuleSlot.objects.filter(daily_slot=_slot(eq, MON, "10:00")).exists()


# --- Skipped slots ------------------------------------------------------------------------------


def test_booked_slots_are_skipped_and_listed_without_refund(admin, eq):
    student = _user(UserType.STUDENT, name="Asha Rao")
    target = _slot(eq, MON + timedelta(days=3), "10:00")
    booking = _book(eq, target, student)

    with patch("iic_booking.equipment.api_views.refund_booking_internal") as refund:
        preview = _client(admin).post(_url(eq, "preview/"), _payload(), format="json").data["preview"]
        data = _create(admin, eq)
    refund.assert_not_called()

    for summary in (preview, data["result"], data["rule"]["summary"]):
        assert summary["skipped_booked_count"] == 1
        row = summary["skipped_booked"][0]
        assert row["booking_reference"] == booking.virtual_booking_id
        assert row["user_name"] == "Asha Rao"
        assert row["date"] == (MON + timedelta(days=3)).isoformat()
        assert (row["start_time"], row["end_time"]) == ("10:00", "11:00")
    target.refresh_from_db()
    booking.refresh_from_db()
    assert target.status == SlotStatus.BOOKED and target.booking_id == booking.pk
    assert booking.status == BookingStatus.BOOKED
    assert data["result"]["blocked_count"] == 7
    assert not RecurringSlotBlockRuleSlot.objects.filter(daily_slot=target).exists()


def test_other_statuses_are_left_unchanged_and_reported(admin, eq):
    day = MON + timedelta(days=7)
    maint = _slot(eq, day, "10:00")
    manual = _slot(eq, day, "14:00")
    absent = _slot(eq, MON + timedelta(days=10), "10:00")
    DailySlot.objects.filter(pk=maint.pk).update(status=SlotStatus.UNDER_MAINTENANCE)
    DailySlot.objects.filter(pk=manual.pk).update(status=SlotStatus.BLOCKED, blocked_label="Service visit")
    DailySlot.objects.filter(pk=absent.pk).update(status=SlotStatus.OPERATOR_ABSENT)

    # Saturdays are generated Not Available (closed day).
    data = _create(admin, eq, weekdays=[MONDAY, THURSDAY, SATURDAY])
    reasons = {(r["date"], r["start_time"]): r for r in data["result"]["skipped_other"]}
    assert reasons[(day.isoformat(), "10:00")]["reason"] == "Under maintenance"
    assert reasons[(day.isoformat(), "14:00")]["reason"] == "Already blocked (Other Reasons)"
    assert reasons[(day.isoformat(), "14:00")]["blocked_label"] == "Service visit"
    assert reasons[((MON + timedelta(days=10)).isoformat(), "10:00")]["reason"] == "Operator absent"
    assert reasons[((MON + timedelta(days=5)).isoformat(), "10:00")]["reason"] == "Not available"
    assert data["result"]["skipped_other_count"] == 3 + 4

    for slot, status, label in (
        (maint, SlotStatus.UNDER_MAINTENANCE, None),
        (manual, SlotStatus.BLOCKED, "Service visit"),
        (absent, SlotStatus.OPERATOR_ABSENT, None),
    ):
        slot.refresh_from_db()
        assert (slot.status, slot.blocked_label) == (status, label)
    assert not RecurringSlotBlockRuleSlot.objects.filter(daily_slot__in=[maint, manual, absent]).exists()


# --- Preview ------------------------------------------------------------------------------------


def _snapshot(eq):
    return sorted(
        DailySlot.objects.filter(slot_master__equipment=eq).values_list("pk", "status", "blocked_label", "booking_id")
    )


def test_preview_writes_nothing(admin, eq):
    before = _snapshot(eq)
    slot_count = DailySlot.objects.count()
    res = _client(admin).post(_url(eq, "preview/"), _payload(end_date=(MON + timedelta(days=60)).isoformat()), format="json")
    assert res.status_code == 200
    preview = res.data["preview"]
    assert preview["to_block_count"] == 8
    assert preview["slots_exist_until"] == (MON + timedelta(days=13)).isoformat()
    # Mon/Thu from day 14 to day 60 that are not generated yet: 14 dates x 2 times.
    assert preview["future_slots_count"] == 28
    assert _snapshot(eq) == before
    assert DailySlot.objects.count() == slot_count
    assert RecurringSlotBlockRule.objects.count() == 0
    assert RecurringSlotBlockRuleSlot.objects.count() == 0


def test_future_count_skips_holidays_and_equipment_under_repair(admin, eq):
    Holiday.objects.create(date=MON + timedelta(days=14), reason="Festival", is_active=True)
    body = _payload(end_date=(MON + timedelta(days=20)).isoformat())
    preview = _client(admin).post(_url(eq, "preview/"), body, format="json").data["preview"]
    assert preview["future_slots_count"] == 2  # day 17 (Thu) only; day 14 is a holiday
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)
    preview = _client(admin).post(_url(eq, "preview/"), body, format="json").data["preview"]
    assert preview["future_slots_count"] == 0


# --- Future generation --------------------------------------------------------------------------


def test_rule_applies_to_slots_generated_later(admin, eq):
    data = _create(admin, eq, end_date=(MON + timedelta(days=27)).isoformat())
    assert data["result"]["future_slots_count"] == 8
    rule_id = data["rule"]["id"]

    _generate(eq, MON + timedelta(days=14), MON + timedelta(days=34))
    third_mon = MON + timedelta(days=14)
    for day in (third_mon, MON + timedelta(days=17), MON + timedelta(days=21), MON + timedelta(days=24)):
        for hhmm in ("10:00", "14:00"):
            slot = _slot(eq, day, hhmm)
            assert (slot.status, slot.blocked_label) == (SlotStatus.BLOCKED, "Calibration")
            assert RecurringSlotBlockRuleSlot.objects.filter(
                rule_id=rule_id, daily_slot=slot, source=RecurringSlotBlockRuleSlot.Source.GENERATED
            ).exists()
    assert _slot(eq, third_mon, "00:30").status == SlotStatus.AVAILABLE
    assert _slot(eq, MON + timedelta(days=15), "10:00").status == SlotStatus.AVAILABLE
    # After the rule's end date and on closed days nothing is blocked.
    assert _slot(eq, MON + timedelta(days=28), "10:00").status == SlotStatus.AVAILABLE
    assert _slot(eq, MON + timedelta(days=19), "10:00").status == SlotStatus.NOT_AVAILABLE

    rule = _client(admin).get(_url(eq, f"{rule_id}/")).data["rule"]
    assert rule["generated_count"] == 8
    assert rule["blocked_now_count"] == 16


def test_rule_applies_to_daily_and_public_generation(admin, eq):
    rule_id = _create(admin, eq, weekdays=[MONDAY], slot_times=["14:00"], end_date=(MON + timedelta(days=40)).isoformat())[
        "rule"
    ]["id"]
    SlotGenerator.generate_daily_slots(eq, MON + timedelta(days=21))
    SlotGenerator.generate_weekly_slots(eq, MON + timedelta(days=28), MON + timedelta(days=34))
    for day in (MON + timedelta(days=21), MON + timedelta(days=28)):
        slot = _slot(eq, day, "14:00")
        assert slot.status == SlotStatus.BLOCKED
        assert RecurringSlotBlockRuleSlot.objects.filter(rule_id=rule_id, daily_slot=slot).exists()


def test_removed_rule_no_longer_applies_to_generation(admin, eq):
    rule_id = _create(admin, eq, end_date=(MON + timedelta(days=27)).isoformat())["rule"]["id"]
    assert _client(admin).post(_url(eq, f"{rule_id}/remove/"), {}, format="json").status_code == 200
    _generate(eq, MON + timedelta(days=14), MON + timedelta(days=20))
    assert _slot(eq, MON + timedelta(days=14), "10:00").status == SlotStatus.AVAILABLE


def test_generation_without_rules_is_unchanged():
    eq = _equipment()
    created = SlotGenerator.generate_slots_for_week(eq, MON, MON + timedelta(days=6))
    assert len(created) == 15  # Mon-Fri x 3 (weekends skipped)
    assert {s.status for s in DailySlot.objects.filter(slot_master__equipment=eq)} == {SlotStatus.AVAILABLE}


# --- Removal ------------------------------------------------------------------------------------


def test_remove_unblocks_only_own_future_unbooked_slots(admin, eq):
    pre_manual = _slot(eq, MON + timedelta(days=7), "10:00")
    DailySlot.objects.filter(pk=pre_manual.pk).update(status=SlotStatus.BLOCKED, blocked_label="Calibration")

    rule_id = _create(admin, eq)["rule"]["id"]
    relabelled = _slot(eq, MON + timedelta(days=3), "10:00")
    booked_later = _slot(eq, MON + timedelta(days=3), "14:00")
    DailySlot.objects.filter(pk=relabelled.pk).update(blocked_label="Service visit")
    # An OIC set it Available and someone booked it while the rule was active.
    DailySlot.objects.filter(pk=booked_later.pk).update(status=SlotStatus.AVAILABLE, blocked_label=None)
    _book(eq, booked_later, _user(UserType.STUDENT))

    preview = _client(admin).get(_url(eq, f"{rule_id}/")).data["rule"]["removal_preview"]
    assert preview == {"will_unblock_count": 5, "kept_by_other_rule_count": 0, "unchanged_count": 2}

    with patch("iic_booking.equipment.waitlist.notify_waitlist_slots_available", return_value=0) as notify:
        res = _client(admin).post(_url(eq, f"{rule_id}/remove/"), {}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["result"]["unblocked_count"] == 5
    notify.assert_called_once()
    assert res.data["rule"]["is_active"] is False

    rule = RecurringSlotBlockRule.objects.get(pk=rule_id)
    assert rule.removed_by_id == admin.pk and rule.removed_at is not None
    for day, hhmm in ((MON, "10:00"), (MON, "14:00"), (MON + timedelta(days=7), "14:00")):
        slot = _slot(eq, day, hhmm)
        assert (slot.status, slot.blocked_label) == (SlotStatus.AVAILABLE, None)
    pre_manual.refresh_from_db()
    relabelled.refresh_from_db()
    booked_later.refresh_from_db()
    assert (pre_manual.status, pre_manual.blocked_label) == (SlotStatus.BLOCKED, "Calibration")
    assert (relabelled.status, relabelled.blocked_label) == (SlotStatus.BLOCKED, "Service visit")
    assert booked_later.status == SlotStatus.BOOKED and booked_later.booking_id

    res = _client(admin).post(_url(eq, f"{rule_id}/remove/"), {}, format="json")
    assert res.status_code == 400 and res.data["code"] == "already_removed"


def test_remove_leaves_started_slots_blocked(admin, eq):
    rule_id = _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00"])["rule"]["id"]
    with patch("django.utils.timezone.now", return_value=datetime(2030, 1, 7, 12, 0, tzinfo=IST)):
        res = _client(admin).post(_url(eq, f"{rule_id}/remove/"), {}, format="json")
    assert res.data["result"]["unblocked_count"] == 1
    assert _slot(eq, MON, "10:00").status == SlotStatus.BLOCKED
    assert _slot(eq, MON + timedelta(days=7), "10:00").status == SlotStatus.AVAILABLE


def test_overlapping_rules_keep_shared_slots_blocked(admin, eq):
    rule_a = _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00", "14:00"], label="Rule A")["rule"]["id"]
    data_b = _create(admin, eq, weekdays=[MONDAY, THURSDAY], slot_times=["14:00"], label="Rule B")
    rule_b = data_b["rule"]["id"]
    assert data_b["result"]["already_blocked_by_rule_count"] == 2
    assert data_b["result"]["blocked_count"] == 2  # the Thursdays

    preview = _client(admin).get(_url(eq, f"{rule_a}/")).data["rule"]["removal_preview"]
    assert preview["will_unblock_count"] == 2 and preview["kept_by_other_rule_count"] == 2

    assert _client(admin).post(_url(eq, f"{rule_a}/remove/"), {}, format="json").status_code == 200
    for day in (MON, MON + timedelta(days=7)):
        assert _slot(eq, day, "10:00").status == SlotStatus.AVAILABLE
        shared = _slot(eq, day, "14:00")
        assert (shared.status, shared.blocked_label) == (SlotStatus.BLOCKED, "Rule B")

    assert _client(admin).post(_url(eq, f"{rule_b}/remove/"), {}, format="json").status_code == 200
    assert {
        s.status for s in DailySlot.objects.filter(slot_master__equipment=eq, date__in=[MON, MON + timedelta(days=3)])
    } == {SlotStatus.AVAILABLE}


def test_generated_slot_covered_by_two_rules_survives_removing_one(admin, eq):
    end = (MON + timedelta(days=27)).isoformat()
    rule_a = _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00"], label="A", end_date=end)["rule"]["id"]
    _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00"], label="B", end_date=end)
    _generate(eq, MON + timedelta(days=14), MON + timedelta(days=20))
    slot = _slot(eq, MON + timedelta(days=14), "10:00")
    assert (slot.status, slot.blocked_label) == (SlotStatus.BLOCKED, "A")
    assert slot.recurring_block_links.count() == 2

    _client(admin).post(_url(eq, f"{rule_a}/remove/"), {}, format="json")
    slot.refresh_from_db()
    assert (slot.status, slot.blocked_label) == (SlotStatus.BLOCKED, "B")


def test_remove_restores_equipment_initial_status(admin, eq):
    rule_id = _create(admin, eq, weekdays=[MONDAY], slot_times=["14:00"])["rule"]["id"]
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)
    _client(admin).post(_url(eq, f"{rule_id}/remove/"), {}, format="json")
    assert _slot(eq, MON + timedelta(days=7), "14:00").status == SlotStatus.UNDER_MAINTENANCE


def test_list_shows_active_rules_and_removed_on_request(admin, eq):
    keep = _create(admin, eq, weekdays=[MONDAY], slot_times=["10:00"])["rule"]["id"]
    gone = _create(admin, eq, weekdays=[THURSDAY], slot_times=["10:00"])["rule"]["id"]
    _client(admin).post(_url(eq, f"{gone}/remove/"), {}, format="json")
    data = _client(admin).get(_url(eq)).data
    assert [r["id"] for r in data["rules"]] == [keep]
    assert data["rules"][0]["weekday_labels"] == ["Mon"]
    data = _client(admin).get(_url(eq) + "?include_removed=1").data
    assert [r["id"] for r in data["removed_rules"]] == [gone]
