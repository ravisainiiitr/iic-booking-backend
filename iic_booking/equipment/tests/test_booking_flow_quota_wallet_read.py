"""Read-only booking-page helpers: my-booking-quota endpoint and extra wallet-balance fields."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
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
    EquipmentGroup,
    EquipmentGroupQuota,
    ExternalUserQuota,
    QuotaLimitType,
    QuotaType,
    SlotMaster,
    SlotStatus,
    UserTypeQuota,
)
from iic_booking.users.models import User
from iic_booking.users.models.department import Department, DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)

IST = ZoneInfo("Asia/Kolkata")

# Wednesday; week is Mon 2026-07-27 .. Sun 2026-08-02, month is July 2026.
VIEW_DAY = date(2026, 7, 29)
IN_WEEK_DAY = date(2026, 7, 28)
PREVIOUS_WEEK_DAY = date(2026, 7, 21)


def _quota_url(equipment, **params):
    url = f"/api/equipments/{equipment.pk}/my-booking-quota/"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(email, user_type, **extra):
    return User.objects.create_user(email=email, password="x", user_type=user_type, **extra)


def _book(equipment, user, *, minutes, day, user_type, status=BookingStatus.BOOKED, hour=9):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment,
        user_type=user_type,
        defaults={
            "primary_unit_charge": Decimal("10.00"),
            "secondary_unit_charge": Decimal("0"),
            "breakpoint": Decimal("99"),
        },
    )
    # DailySlot is unique per (slot_master, date), so use one slot master per start hour.
    slot_master, _ = SlotMaster.objects.get_or_create(
        equipment=equipment,
        slot_number=hour,
        defaults={
            "slot_name": f"S{hour}",
            "open_time": datetime.strptime(f"{hour:02d}:00", "%H:%M").time(),
            "close_time": datetime.strptime(f"{hour + 1:02d}:00", "%H:%M").time(),
            "is_active": True,
        },
    )
    start = timezone.make_aware(datetime(day.year, day.month, day.day, hour, 0), IST)
    booking = Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        user_type_snapshot=user_type,
        total_time_minutes=minutes,
        total_charge=Decimal("10.00"),
        status=status,
        quota_period_anchor_at=start,
    )
    DailySlot.objects.create(
        slot_master=slot_master,
        date=day,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=minutes),
        status=SlotStatus.BOOKED,
        booking=booking,
    )
    return booking


@pytest.fixture
def quota_enforced(settings):
    settings.SKIP_BOOKING_QUOTA_CHECK = False


@pytest.fixture
def group_equipment(db, quota_enforced):
    group = EquipmentGroup.objects.create(name="XRD Group")
    equipment = Equipment.objects.create(
        name="XRD One",
        code="XRD1",
        equipment_group=group,
        slot_duration_minutes=60,
        skip_quota_check=False,
    )
    EquipmentGroupQuota.objects.create(
        equipment_group=group,
        quota_type=QuotaType.WEEKLY,
        internal_individual_quota_minutes=120,
        internal_faculty_quota_minutes=240,
        external_individual_quota_minutes=60,
        external_faculty_quota_minutes=120,
        is_enforced=True,
    )
    EquipmentGroupQuota.objects.create(
        equipment_group=group,
        quota_type=QuotaType.MONTHLY,
        internal_individual_quota_minutes=300,
        internal_faculty_quota_minutes=480,
        external_individual_quota_minutes=120,
        external_faculty_quota_minutes=240,
        is_enforced=True,
    )
    return equipment


def _by_scope(data):
    return {p["scope"]: p for p in data["periods"]}


# ---------------------------------------------------------------------------
# my-booking-quota
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_quota_student_individual_periods_and_binding(group_equipment):
    student = _user("q.student@test.local", UserType.STUDENT)
    _book(group_equipment, student, minutes=60, day=IN_WEEK_DAY, user_type=UserType.STUDENT)
    _book(group_equipment, student, minutes=30, day=PREVIOUS_WEEK_DAY, user_type=UserType.STUDENT)
    _book(
        group_equipment, student, minutes=90, day=IN_WEEK_DAY,
        user_type=UserType.STUDENT, status=BookingStatus.CANCELLED, hour=11,
    )

    resp = _client(student).get(_quota_url(group_equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200, resp.data
    data = resp.data
    assert data["applies"] is True
    assert data["equipment_id"] == group_equipment.pk
    assert data["equipment_name"] == "XRD One"
    assert data["equipment_group_name"] == "XRD Group"
    assert data["reference_date"] == "2026-07-29"
    periods = _by_scope(data)
    assert set(periods) == {"Individual Monthly", "Individual Weekly"}

    weekly = periods["Individual Weekly"]
    assert weekly["period"] == QuotaType.WEEKLY
    assert weekly["shared"] is False
    assert (weekly["limit_minutes"], weekly["used_minutes"], weekly["remaining_minutes"]) == (120, 60, 60)
    assert weekly["period_start"].startswith("2026-07-27T00:00:00")
    assert weekly["period_end"].startswith("2026-08-02T23:59:59")

    monthly = periods["Individual Monthly"]
    assert (monthly["limit_minutes"], monthly["used_minutes"], monthly["remaining_minutes"]) == (300, 90, 210)
    assert monthly["period_start"].startswith("2026-07-01")

    assert data["remaining_minutes"] == 60
    assert data["binding"]["scope"] == "Individual Weekly"


@pytest.mark.django_db
def test_date_param_selects_the_week(group_equipment):
    student = _user("q.week@test.local", UserType.STUDENT)
    _book(group_equipment, student, minutes=60, day=IN_WEEK_DAY, user_type=UserType.STUDENT)
    _book(group_equipment, student, minutes=30, day=PREVIOUS_WEEK_DAY, user_type=UserType.STUDENT)

    resp = _client(student).get(_quota_url(group_equipment, date="2026-07-22"))

    assert resp.status_code == 200
    weekly = _by_scope(resp.data)["Individual Weekly"]
    assert weekly["used_minutes"] == 30
    assert weekly["remaining_minutes"] == 90
    assert weekly["period_start"].startswith("2026-07-20")
    assert _by_scope(resp.data)["Individual Monthly"]["used_minutes"] == 90


@pytest.mark.django_db
def test_faculty_wallet_student_gets_faculty_and_individual_periods(group_equipment):
    student = _user("q.fw.student@test.local", UserType.STUDENT)
    faculty = _user("q.fw.faculty@test.local", UserType.FACULTY)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    _book(group_equipment, student, minutes=60, day=IN_WEEK_DAY, user_type=UserType.STUDENT)
    _book(group_equipment, faculty, minutes=150, day=IN_WEEK_DAY, user_type=UserType.FACULTY, hour=12)

    resp = _client(student).get(_quota_url(group_equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    periods = _by_scope(resp.data)
    assert [p["scope"] for p in resp.data["periods"]] == [
        "Faculty Monthly",
        "Faculty Weekly",
        "Individual Monthly",
        "Individual Weekly",
    ]
    fw = periods["Faculty Weekly"]
    assert fw["shared"] is True
    assert (fw["limit_minutes"], fw["used_minutes"], fw["remaining_minutes"]) == (240, 210, 30)
    assert periods["Individual Weekly"]["used_minutes"] == 60
    assert resp.data["remaining_minutes"] == 30
    assert resp.data["binding"]["scope"] == "Faculty Weekly"


@pytest.mark.django_db
def test_faculty_only_gets_faculty_periods(group_equipment):
    faculty = _user("q.fac@test.local", UserType.FACULTY)
    Wallet.objects.create(user=faculty)

    resp = _client(faculty).get(_quota_url(group_equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert {p["scope"] for p in resp.data["periods"]} == {"Faculty Monthly", "Faculty Weekly"}


@pytest.mark.django_db
def test_admin_self_is_not_subject_to_quota(group_equipment):
    admin = _user("q.admin@test.local", UserType.ADMIN)

    resp = _client(admin).get(_quota_url(group_equipment))

    assert resp.status_code == 200
    assert resp.data["applies"] is False
    assert resp.data["reason"] == "staff"
    assert resp.data["periods"] == []
    assert resp.data["remaining_minutes"] is None
    assert resp.data["binding"] is None
    assert resp.data["reference_date"] == timezone.localdate().isoformat()


@pytest.mark.django_db
def test_admin_can_query_student_by_user_id(group_equipment):
    admin = _user("q.admin2@test.local", UserType.ADMIN)
    student = _user("q.target@test.local", UserType.STUDENT)
    _book(group_equipment, student, minutes=45, day=IN_WEEK_DAY, user_type=UserType.STUDENT)

    resp = _client(admin).get(_quota_url(group_equipment, date=VIEW_DAY.isoformat(), user_id=student.pk))

    assert resp.status_code == 200
    assert resp.data["applies"] is True
    assert _by_scope(resp.data)["Individual Weekly"]["used_minutes"] == 45


@pytest.mark.django_db
def test_skip_quota_equipment_does_not_apply(quota_enforced):
    equipment = Equipment.objects.create(name="Skip EQ", code="SKIPQ", skip_quota_check=True)
    student = _user("q.skip@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment))

    assert resp.status_code == 200
    assert resp.data["applies"] is False
    assert resp.data["reason"] == "skipped"
    assert resp.data["equipment_group_name"] is None


@pytest.mark.django_db
def test_legacy_user_type_hours_quota(quota_enforced):
    equipment = Equipment.objects.create(name="Solo EQ", code="SOLOQ", skip_quota_check=False)
    UserTypeQuota.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        quota_type=QuotaType.MONTHLY,
        limit_type=QuotaLimitType.HOURS,
        limit_value=Decimal("200"),
        is_enforced=True,
    )
    UserTypeQuota.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        quota_type=QuotaType.WEEKLY,
        limit_type=QuotaLimitType.BOOKINGS,
        limit_value=Decimal("1"),
        is_enforced=True,
    )
    student = _user("q.legacy@test.local", UserType.STUDENT)
    _book(equipment, student, minutes=60, day=IN_WEEK_DAY, user_type=UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    assert resp.data["applies"] is True
    assert len(resp.data["periods"]) == 1
    period = resp.data["periods"][0]
    assert period["scope"] == "Individual Monthly"
    assert period["period"] == QuotaType.MONTHLY
    assert (period["limit_minutes"], period["used_minutes"], period["remaining_minutes"]) == (200, 60, 140)
    assert resp.data["binding"]["scope"] == "Individual Monthly"


@pytest.mark.django_db
def test_legacy_external_hours_quota(quota_enforced):
    equipment = Equipment.objects.create(name="Ext EQ", code="EXTQQ", skip_quota_check=False)
    ExternalUserQuota.objects.create(
        equipment=equipment,
        quota_type=QuotaType.WEEKLY,
        limit_type=QuotaLimitType.HOURS,
        limit_value=Decimal("90"),
        is_enforced=True,
    )
    external = _user("q.ext@test.local", UserType.EXTERNAL)
    _book(equipment, external, minutes=120, day=IN_WEEK_DAY, user_type=UserType.EXTERNAL)

    resp = _client(external).get(_quota_url(equipment, date=VIEW_DAY.isoformat()))

    assert resp.status_code == 200
    period = resp.data["periods"][0]
    assert period["scope"] == "External Weekly"
    assert (period["used_minutes"], period["remaining_minutes"]) == (120, 0)
    assert resp.data["remaining_minutes"] == 0


@pytest.mark.django_db
def test_no_configured_limits(quota_enforced):
    equipment = Equipment.objects.create(name="Free EQ", code="FREEQ", skip_quota_check=False)
    student = _user("q.free@test.local", UserType.STUDENT)

    resp = _client(student).get(_quota_url(equipment))

    assert resp.status_code == 200
    assert resp.data["applies"] is False
    assert resp.data["reason"] == "no_limits"


@pytest.mark.django_db
def test_bad_date_returns_400(group_equipment):
    student = _user("q.bad@test.local", UserType.STUDENT)
    resp = _client(student).get(_quota_url(group_equipment, date="29-07-2026"))
    assert resp.status_code == 400


@pytest.mark.django_db
def test_non_staff_user_id_returns_403(group_equipment):
    student = _user("q.nosy@test.local", UserType.STUDENT)
    other = _user("q.other@test.local", UserType.STUDENT)
    resp = _client(student).get(_quota_url(group_equipment, user_id=other.pk))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_unknown_equipment_returns_404(quota_enforced):
    student = _user("q.404@test.local", UserType.STUDENT)
    resp = _client(student).get("/api/equipments/987654/my-booking-quota/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_requires_authentication(group_equipment):
    resp = APIClient().get(_quota_url(group_equipment))
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# wallet/equipment-department-balance additions
# ---------------------------------------------------------------------------

BALANCE_URL = "/api/wallet/equipment-department-balance/"


@pytest.fixture
def dept_equipment(db):
    dept = Department.objects.create(name="Physics", code="PHY", department_type=DepartmentType.INTERNAL)
    equipment = Equipment.objects.create(name="SEM", code="SEMQ", internal_department=dept)
    return dept, equipment


@pytest.mark.django_db
def test_student_without_wallet_needs_link(dept_equipment):
    _dept, equipment = dept_equipment
    student = _user("w.nolink@test.local", UserType.STUDENT)

    resp = _client(student).get(BALANCE_URL, {"equipment_id": equipment.pk})

    assert resp.status_code == 200
    data = resp.data
    assert data["has_wallet"] is False
    assert data["balance"] == "0.00"
    assert data["is_zero"] is True
    assert data["department_code"] == "PHY"
    assert data["needs_wallet_link"] is True
    assert data["pending_link_request"] is False
    assert data["pending_link_supervisor_name"] is None
    assert data["spendable"] == "0.00"
    assert data["booking_block_message"] is None
    assert data["pays_remainder_separately"] is False


@pytest.mark.django_db
def test_student_with_pending_link_request(dept_equipment):
    _dept, equipment = dept_equipment
    student = _user("w.pending@test.local", UserType.STUDENT)
    faculty = _user("w.sup@test.local", UserType.FACULTY, name="Dr Supervisor")
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.PENDING
    )

    resp = _client(student).get(BALANCE_URL, {"equipment_id": equipment.pk})

    assert resp.status_code == 200
    assert resp.data["has_wallet"] is False
    assert resp.data["needs_wallet_link"] is True
    assert resp.data["pending_link_request"] is True
    assert resp.data["pending_link_supervisor_name"] == "Dr Supervisor"


@pytest.mark.django_db
def test_student_with_approved_link_has_spendable(dept_equipment):
    dept, equipment = dept_equipment
    student = _user("w.linked@test.local", UserType.STUDENT)
    faculty = _user("w.fac@test.local", UserType.FACULTY)
    wallet = Wallet.objects.create(user=faculty)
    SubWallet.objects.create(wallet=wallet, department=dept, balance=Decimal("500.00"))
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )

    resp = _client(student).get(BALANCE_URL, {"equipment_id": equipment.pk})

    assert resp.status_code == 200
    data = resp.data
    assert data["has_wallet"] is True
    assert data["balance"] == "500.00"
    assert data["spendable"] == "500.00"
    assert data["needs_wallet_link"] is False
    assert data["pending_link_request"] is False
    assert data["booking_block_message"] is None
    assert data["pays_remainder_separately"] is False


@pytest.mark.django_db
def test_external_user_pays_remainder_separately(dept_equipment):
    dept, equipment = dept_equipment
    external = _user("w.ext@test.local", UserType.EXTERNAL)
    wallet = Wallet.objects.create(user=external)
    SubWallet.objects.create(wallet=wallet, department=dept, balance=Decimal("25.50"))

    resp = _client(external).get(BALANCE_URL, {"equipment_id": equipment.pk})

    assert resp.status_code == 200
    assert resp.data["has_wallet"] is True
    assert resp.data["spendable"] == "25.50"
    assert resp.data["needs_wallet_link"] is False
    assert resp.data["pays_remainder_separately"] is True
