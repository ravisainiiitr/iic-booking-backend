"""Admin / Department Administrator / (temporary) OIC bookings and moves for a user skip weekly / monthly limits."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService
from iic_booking.equipment.models import (
    Booking,
    EquipmentManager,
    EquipmentTemporaryOIC,
    QuotaLimitType,
    QuotaType,
    UserTypeQuota,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory
from iic_booking.users.student_spending_limits import IST


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _staff(user_type, department, **extra):
    return UserFactory(user_type=user_type, department=department, admin_approved=True, **extra)


@pytest.fixture
def lab(egs_factory, settings, no_portal_lock):
    """A student who has used their weekly limit of one booking on an equipment, plus staff of every kind."""
    settings.SKIP_BOOKING_QUOTA_CHECK = False
    f = egs_factory
    eq = f.equipment(skip_quota_check=False)
    other_eq = f.equipment(skip_quota_check=False)
    UserTypeQuota.objects.create(
        equipment=eq,
        user_type=UserType.STUDENT,
        quota_type=QuotaType.WEEKLY,
        limit_type=QuotaLimitType.BOOKINGS,
        limit_value=Decimal("1"),
        is_enforced=True,
    )
    student = f.student()
    faculty = _staff(UserType.FACULTY, f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        status=WalletJoinRequestStatus.APPROVED,
        responded_at=datetime(2026, 1, 1, tzinfo=IST),
    )
    SubWalletRepository.get_or_create(wallet, f.department).credit(Decimal("10000.00"), description="Recharge")
    used = f.booking(student, eq, f.future(days=4, hour=9))

    oic = _staff(UserType.MANAGER, f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    temp_oic = _staff(UserType.MANAGER, f.department)
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp_oic, resume_at=timezone.now() + timedelta(days=3)
    )
    other_oic = _staff(UserType.MANAGER, f.department)
    EquipmentManager.objects.create(equipment=other_eq, manager=other_oic)
    other_dept = Department.objects.create(name="Other Dept SQ", code="OSQ1")
    return SimpleNamespace(
        f=f,
        eq=eq,
        student=student,
        used=used,
        admin=_staff(UserType.ADMIN, f.department),
        oic=oic,
        temp_oic=temp_oic,
        dept_admin=_staff(UserType.DEPT_ADMIN, f.department),
        other_oic=other_oic,
        other_dept_admin=_staff(UserType.DEPT_ADMIN, other_dept),
    )


def _book(lab, actor, *, hour, on_behalf=True):
    slot = lab.f.slot(lab.eq, lab.f.future(days=4, hour=hour))
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
    }
    if on_behalf:
        body["user_id"] = lab.student.pk
    return lab.f.client_for(actor).post(f"/api/equipments/{lab.eq.pk}/book/", body, format="json")


@pytest.mark.django_db
def test_user_over_weekly_limit_is_still_blocked(lab):
    resp = _book(lab, lab.student, hour=13, on_behalf=False)

    assert resp.status_code == 400, resp.data
    assert "quota" in resp.data["error"].lower()
    assert Booking.objects.filter(user=lab.student).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["admin", "oic", "temp_oic", "dept_admin"])
def test_staff_booking_for_user_skips_weekly_limit_and_still_counts(lab, role):
    resp = _book(lab, getattr(lab, role), hour=13)

    assert resp.status_code in (200, 201), resp.data
    assert Booking.objects.filter(user=lab.student).count() == 2

    # The staff booking counts toward the user's own later bookings.
    resp = _book(lab, lab.student, hour=15, on_behalf=False)
    assert resp.status_code == 400, resp.data
    assert Booking.objects.filter(user=lab.student).count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["other_oic", "other_dept_admin"])
def test_staff_without_rights_on_the_equipment_get_no_bypass(lab, role):
    resp = _book(lab, getattr(lab, role), hour=13)

    assert resp.status_code == 403, resp.data
    assert Booking.objects.filter(user=lab.student).count() == 1


@pytest.mark.django_db
def test_helper_needs_another_user_and_rights_on_the_equipment(lab):
    from iic_booking.equipment.api_views import staff_booking_skips_period_limits

    assert staff_booking_skips_period_limits(lab.oic, lab.eq, lab.student) is True
    assert staff_booking_skips_period_limits(lab.dept_admin, lab.eq, lab.student) is True
    assert staff_booking_skips_period_limits(lab.oic, lab.eq, lab.oic) is False
    assert staff_booking_skips_period_limits(lab.other_oic, lab.eq, lab.student) is False
    assert staff_booking_skips_period_limits(lab.other_dept_admin, lab.eq, lab.student) is False
    assert staff_booking_skips_period_limits(lab.student, lab.eq, lab.f.student()) is False


# --- reschedule into a week where the user's limit is already used ------------------------------


@pytest.fixture
def move(lab, egs_quiet_side_effects, monkeypatch):
    """The student's booking next week uses that week's limit; ``lab.used`` is moved into it."""
    lab.f.booking(lab.student, lab.eq, lab.f.future(days=11, hour=9))
    target = lab.f.slot(lab.eq, lab.f.future(days=11, hour=13))
    bypasses = []

    def _spy(cls, *args, bypass=False, **kwargs):
        bypasses.append(bypass)
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(ExternalSlotQuotaService, "validate_external_booking", classmethod(_spy))
    body = {"start_time": target.start_datetime.isoformat(), "end_time": target.end_datetime.isoformat()}
    return SimpleNamespace(target=target, body=body, bypasses=bypasses)


def _moved_to_target(lab, move):
    return list(lab.used.daily_slots.values_list("pk", flat=True)) == [move.target.pk]


@pytest.mark.django_db
def test_user_reschedule_into_full_week_is_blocked(lab, move):
    resp = lab.f.client_for(lab.student).post(
        f"/api/bookings/{lab.used.pk}/user-reschedule/", move.body, format="json"
    )

    assert resp.status_code == 400, resp.data
    assert "quota" in resp.data["error"].lower()
    assert not _moved_to_target(lab, move)


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["admin", "oic", "temp_oic", "dept_admin"])
@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_staff_reschedule_into_full_week_succeeds(lab, move, role, endpoint, monkeypatch):
    if role == "dept_admin":
        # Department Administrators reschedule only with the Bookings module granted.
        monkeypatch.setattr("iic_booking.users.rbac.user_has_permission", lambda user, code, *a, **k: True)
    resp = lab.f.client_for(getattr(lab, role)).post(
        f"/api/bookings/{lab.used.pk}/{endpoint}/", move.body, format="json"
    )

    assert resp.status_code == 200, resp.data
    assert _moved_to_target(lab, move)
    assert move.bypasses == [True]


@pytest.mark.django_db
def test_oic_of_other_equipment_gets_no_external_quota_bypass(lab, move):
    lab.f.client_for(lab.other_oic).post(f"/api/bookings/{lab.used.pk}/user-reschedule/", move.body, format="json")

    assert move.bypasses == [False]
