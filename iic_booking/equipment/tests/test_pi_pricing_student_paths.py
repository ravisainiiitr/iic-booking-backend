"""Students on an Equipment PI's wallet pay the PI rates on every pricing path, and nobody else does."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache

from iic_booking.equipment.models import (
    Booking,
    ChargeProfile,
    ChargeProfilePricingProfile,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
    EquipmentPI,
    EquipmentProfileType,
)
from iic_booking.equipment.pi_pricing import category_estimate_pricing_profile, pi_rate_user_types_by_equipment
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PI = ChargeProfilePricingProfile.PI
STANDARD = ChargeProfilePricingProfile.STANDARD
STANDARD_RATE = Decimal("100.00")
PI_RATE = Decimal("20.00")


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


class _World:
    def __init__(self, egs):
        cache.clear()
        self.egs = egs
        self.eq = self.equipment()
        self.other_eq = self.equipment()
        self.pi = self.faculty()
        self.pi2 = self.faculty()
        EquipmentPI.objects.create(equipment=self.eq, faculty=self.pi)
        EquipmentPI.objects.create(equipment=self.eq, faculty=self.pi2)
        self.other_eq_pi = self.faculty()
        EquipmentPI.objects.create(equipment=self.other_eq, faculty=self.other_eq_pi)
        self.oic = UserFactory(user_type=UserType.MANAGER, department=egs.department, admin_approved=True)
        EquipmentManager.objects.create(equipment=self.eq, manager=self.oic)

    def equipment(self):
        eq = self.egs.equipment(time_formula="A*60", unit_charge=str(STANDARD_RATE))
        DynamicInputField.objects.create(
            equipment=eq, field_key="A", field_label="No. of Samples",
            field_type=DynamicInputFieldType.NUMERIC, options={"min": 1, "max": 10}, editing_required=False,
        )
        for user_type, pricing, rate in (
            (UserType.FACULTY, STANDARD, STANDARD_RATE),
            (UserType.FACULTY, PI, PI_RATE),
        ):
            ChargeProfile.objects.create(
                equipment=eq, user_type=user_type, pricing_profile=pricing,
                profile_type=EquipmentProfileType.HOUR, time_formula="A*60", primary_unit_charge=rate,
            )
        return eq

    def faculty(self):
        faculty = UserFactory(user_type=UserType.FACULTY, department=self.egs.department, admin_approved=True)
        wallet = Wallet.objects.create(user=faculty)
        SubWalletRepository.get_or_create(wallet, self.egs.department).credit(Decimal("100000"), description="Recharge")
        return faculty

    def student_of(self, faculty):
        student = self.egs.student()
        WalletJoinRequest.objects.create(
            student=student, faculty=faculty, wallet=faculty.wallet, status=WalletJoinRequestStatus.APPROVED
        )
        return student

    def calculate(self, actor, **params):
        res = self.egs.client_for(actor).get(f"/api/equipments/{self.eq.pk}/calculate/", {"A": "2", **params})
        assert res.status_code == 200, res.data
        return res.data

    def book(self, actor, day, **extra):
        slots = [self.egs.slot(self.eq, self.egs.future(days=day, hour=h)) for h in (10, 11)]
        res = self.egs.client_for(actor).post(
            f"/api/equipments/{self.eq.pk}/book/",
            {
                "slot_ids": [s.pk for s in slots],
                "start_time": slots[0].start_datetime.isoformat(),
                "end_time": slots[-1].end_datetime.isoformat(),
                "input_values": {"A": 2},
                **extra,
            },
            format="json",
        )
        assert res.status_code in (200, 201), res.data
        return Booking.objects.filter(equipment=self.eq).order_by("-booking_id").first()


@pytest.fixture
def world(egs_factory):
    return _World(egs_factory)


def test_preview_and_booking_use_pi_rates_for_student_on_pi_wallet(world, no_portal_lock):
    student = world.student_of(world.pi)
    preview = world.calculate(student)
    assert preview["applied_profile"] == "PI"
    assert Decimal(preview["total_charge"]) == 2 * PI_RATE
    assert Decimal(preview["normal_charge"]) == 2 * STANDARD_RATE

    booking = world.book(student, day=2)
    assert booking.charge_profile.pricing_profile == PI
    assert booking.total_charge == Decimal(preview["total_charge"])


def test_every_pi_of_the_equipment_counts(world, no_portal_lock):
    for faculty in (world.pi, world.pi2):
        student = world.student_of(faculty)
        assert world.calculate(student)["applied_profile"] == "PI"
    booking = world.book(world.student_of(world.pi2), day=3)
    assert booking.total_charge == 2 * PI_RATE


def test_unlinked_students_and_pi_of_other_equipment_pay_standard(world, no_portal_lock):
    loose = world.egs.student()
    on_other_faculty = world.student_of(world.faculty())
    on_other_equipment_pi = world.student_of(world.other_eq_pi)
    for student in (loose, on_other_faculty, on_other_equipment_pi):
        preview = world.calculate(student)
        assert preview["applied_profile"] == "Normal"
        assert Decimal(preview["total_charge"]) == 2 * STANDARD_RATE
    booking = world.book(on_other_equipment_pi, day=4)
    assert booking.charge_profile.pricing_profile == STANDARD
    assert booking.total_charge == 2 * STANDARD_RATE


def test_pending_wallet_join_does_not_unlock_pi_rates(world):
    student = world.egs.student()
    WalletJoinRequest.objects.create(
        student=student, faculty=world.pi, wallet=world.pi.wallet, status=WalletJoinRequestStatus.PENDING
    )
    assert world.calculate(student)["applied_profile"] == "Normal"


def test_inactive_pi_assignment_stops_pi_rates(world):
    student = world.student_of(world.pi)
    EquipmentPI.objects.filter(equipment=world.eq, faculty=world.pi).update(is_active=False)
    assert world.calculate(student)["applied_profile"] == "Normal"


def test_calculate_charges_own_category_matches_booking(world):
    student = world.student_of(world.pi)
    own = world.calculate(student, user_type=UserType.STUDENT)
    assert own["applied_profile"] == "PI"
    assert Decimal(own["total_charge"]) == 2 * PI_RATE
    assert world.calculate(world.pi, user_type=UserType.FACULTY)["applied_profile"] == "PI"

    other = world.calculate(student, user_type=UserType.FACULTY)
    assert other["applied_profile"] == "Normal"
    assert Decimal(other["total_charge"]) == 2 * STANDARD_RATE
    loose = world.calculate(world.egs.student(), user_type=UserType.STUDENT)
    assert Decimal(loose["total_charge"]) == 2 * STANDARD_RATE


def test_category_estimate_pricing_profile_rules(world):
    student = world.student_of(world.pi)
    assert category_estimate_pricing_profile(student, world.eq, "STUDENT") == PI
    assert category_estimate_pricing_profile(student, world.eq, UserType.FACULTY) == STANDARD
    assert category_estimate_pricing_profile(student, world.other_eq, UserType.STUDENT) == STANDARD
    assert category_estimate_pricing_profile(None, world.eq, UserType.STUDENT) == STANDARD


def test_oic_on_behalf_preview_and_booking(world, no_portal_lock):
    student = world.student_of(world.pi)
    preview = world.calculate(world.oic, user_id=student.pk)
    assert preview["applied_profile"] == "PI"
    assert Decimal(preview["total_charge"]) == 2 * PI_RATE

    booking = world.book(world.oic, day=5, user_id=student.pk)
    assert booking.user_id == student.pk
    assert booking.charge_profile.pricing_profile == PI
    assert booking.total_charge == 2 * PI_RATE

    loose = world.egs.student()
    assert world.calculate(world.oic, user_id=loose.pk)["applied_profile"] == "Normal"


def test_urgent_preview_surcharges_the_pi_rate(world):
    student = world.student_of(world.pi)
    urgent = world.calculate(student, urgent="1")
    assert urgent["applied_profile"] == "PI"
    assert Decimal(urgent["base_charge"]) == 2 * PI_RATE * Decimal("1.5")
    assert Decimal(urgent["urgent_surcharge_amount"]) == 2 * PI_RATE * Decimal("0.5")


def test_recalculation_after_input_edit_stays_on_pi_rates(world, no_portal_lock):
    from iic_booking.equipment.api_views import _calculate_input_values_charge

    booking = world.book(world.student_of(world.pi), day=6)
    assert _calculate_input_values_charge(booking, {"A": 3}) == 3 * PI_RATE


def test_waitlist_and_group_resolvers_use_pi_rates(world):
    from iic_booking.equipment.equipment_group_service import resolve_charge_profile
    from iic_booking.equipment.waitlist_booking import _resolve_charge_profile_for_user

    student = world.student_of(world.pi)
    profile, _user_type, _external = _resolve_charge_profile_for_user(world.eq, student)
    assert profile.pricing_profile == PI
    assert resolve_charge_profile(student, world.eq).pricing_profile == PI
    loose, _ut, _ext = _resolve_charge_profile_for_user(world.eq, world.egs.student())
    assert loose.pricing_profile == STANDARD


def test_catalog_from_price_shows_pi_rate(world):
    student = world.student_of(world.pi)
    rows = {
        r["equipment_id"]: r
        for r in world.egs.client_for(student).get("/api/equipments/").data["equipments"]
    }
    assert rows[world.eq.pk]["from_price"] == f"{PI_RATE:.2f}"
    assert rows[world.other_eq.pk]["from_price"] == f"{STANDARD_RATE:.2f}"

    assert pi_rate_user_types_by_equipment(student, [world.eq.pk, world.other_eq.pk]) == {
        world.eq.pk: UserType.FACULTY
    }
    assert pi_rate_user_types_by_equipment(world.egs.student(), [world.eq.pk]) == {}

    EquipmentPI.objects.create(equipment=world.other_eq, faculty=world.pi)
    assert set(pi_rate_user_types_by_equipment(student, [world.eq.pk, world.other_eq.pk])) == {
        world.eq.pk, world.other_eq.pk
    }
