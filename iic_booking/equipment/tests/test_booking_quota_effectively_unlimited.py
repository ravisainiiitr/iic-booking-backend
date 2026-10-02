"""Weekly/monthly limits too large to ever be used up are hidden from my-booking-quota (still enforced)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.models import (
    Equipment,
    EquipmentGroup,
    EquipmentGroupQuota,
    QuotaLimitType,
    QuotaType,
    UserTypeQuota,
)
from iic_booking.equipment.quota_utils import QuotaService, quota_limit_is_effectively_unlimited
from iic_booking.equipment.tests.test_booking_flow_quota_wallet_read import (
    IN_WEEK_DAY,
    VIEW_DAY,
    _book,
    _client,
    _quota_url,
    _user,
)
from iic_booking.users.models.user_type import UserType


@pytest.fixture
def quota_enforced(settings):
    settings.SKIP_BOOKING_QUOTA_CHECK = False


def _group_equipment(*, weekly: int, monthly: int | None = None) -> Equipment:
    group = EquipmentGroup.objects.create(name="ICPMS Group")
    equipment = Equipment.objects.create(
        name="Inductively Coupled Plasma Mass Spectrometry (ICPMS-MS)",
        code="ICPMS",
        equipment_group=group,
        slot_duration_minutes=60,
        skip_quota_check=False,
    )
    for quota_type, minutes in ((QuotaType.WEEKLY, weekly), (QuotaType.MONTHLY, monthly)):
        if minutes is None:
            continue
        EquipmentGroupQuota.objects.create(
            equipment_group=group,
            quota_type=quota_type,
            internal_individual_quota_minutes=minutes,
            internal_faculty_quota_minutes=minutes,
            external_individual_quota_minutes=minutes,
            external_faculty_quota_minutes=minutes,
            is_enforced=True,
        )
    return equipment


@pytest.mark.parametrize(
    ("quota_type", "minutes", "expected"),
    [
        (QuotaType.WEEKLY, 10075, True),
        (QuotaType.WEEKLY, 10080, True),
        (QuotaType.WEEKLY, 9000, True),
        (QuotaType.WEEKLY, 8999, False),
        (QuotaType.WEEKLY, 600, False),
        (QuotaType.WEEKLY, 0, False),
        (QuotaType.MONTHLY, 44640, True),
        (QuotaType.MONTHLY, 40000, True),
        (QuotaType.MONTHLY, 39999, False),
        (QuotaType.MONTHLY, 10075, False),
        ("DAILY", 99999, False),
        (QuotaType.WEEKLY, None, False),
    ],
)
def test_effectively_unlimited_thresholds(quota_type, minutes, expected):
    assert quota_limit_is_effectively_unlimited(quota_type, minutes) is expected


@pytest.mark.django_db
def test_weekly_10075_is_hidden(quota_enforced):
    equipment = _group_equipment(weekly=10075)
    student = _user("u.week@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert resp.data["applies"] is False
    assert resp.data["reason"] == "no_limits"
    assert resp.data["periods"] == []
    assert resp.data["binding"] is None


@pytest.mark.django_db
def test_realistic_weekly_limit_still_shown(quota_enforced):
    equipment = _group_equipment(weekly=600)
    student = _user("u.real@test.local", UserType.STUDENT)
    _book(equipment, student, minutes=120, day=IN_WEEK_DAY, user_type=UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert resp.data["applies"] is True
    assert resp.data["binding"]["scope"] == "Individual Weekly"
    assert (resp.data["binding"]["limit_minutes"], resp.data["remaining_minutes"]) == (600, 480)


@pytest.mark.django_db
def test_monthly_unlimited_hidden_but_realistic_weekly_kept(quota_enforced):
    equipment = _group_equipment(weekly=600, monthly=44000)
    student = _user("u.month@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert [p["scope"] for p in resp.data["periods"]] == ["Individual Weekly"]
    assert resp.data["binding"]["limit_minutes"] == 600


@pytest.mark.django_db
def test_realistic_monthly_shown_when_weekly_unlimited(quota_enforced):
    equipment = _group_equipment(weekly=10075, monthly=2400)
    student = _user("u.mix@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert [p["scope"] for p in resp.data["periods"]] == ["Individual Monthly"]
    assert resp.data["remaining_minutes"] == 2400


@pytest.mark.django_db
def test_legacy_monthly_hours_quota_unlimited_hidden(quota_enforced):
    equipment = Equipment.objects.create(name="Legacy EQ", code="LEGUNL", skip_quota_check=False)
    UserTypeQuota.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        quota_type=QuotaType.MONTHLY,
        limit_type=QuotaLimitType.HOURS,
        limit_value=Decimal("44640"),
        is_enforced=True,
    )
    student = _user("u.legacy@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert resp.data["applies"] is False
    assert resp.data["reason"] == "no_limits"


@pytest.mark.django_db
def test_enforcement_unchanged_for_unlimited_limit(quota_enforced):
    equipment = _group_equipment(weekly=10075)
    student = _user("u.enf@test.local", UserType.STUDENT)

    ok, _ = QuotaService.validate_booking_quota(student, equipment, additional_time_minutes=10000)
    blocked, err = QuotaService.validate_booking_quota(student, equipment, additional_time_minutes=10076)

    assert ok is True
    assert blocked is False
    assert "Individual Weekly quota exceeded" in err
