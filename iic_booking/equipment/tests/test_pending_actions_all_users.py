"""Attention items for every user type, and the in-app notifications behind them."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.models import (
    Booking,
    BookingDataShare,
    BookingResultView,
    BookingStatus,
    ChargeProfile,
    Equipment,
    EquipmentManager,
    Semester,
    StudentEquipmentNomination,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.wallet_credit_facility import WalletCreditFacility, WalletCreditFacilityStatus
from iic_booking.users.tests.factories import UserFactory
from iic_booking.users.wallet_credit_facility_v2 import return_for_clarification

pytestmark = pytest.mark.django_db


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _internal_dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=f"PA-{tag}", code=f"PA{tag[:4]}", department_type="internal")


def _equipment(**kwargs):
    defaults = {
        "name": "Attention EQ",
        "code": f"PA{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(owner, equipment, status=BookingStatus.COMPLETED):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        completed_at=timezone.now() if status == BookingStatus.COMPLETED else None,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


def _items(user):
    return {i["key"]: i for i in collect_pending_actions(user)}


def _bell(user):
    return list(
        CommunicationLog.objects.filter(
            recipient=user, communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION
        ).order_by("id")
    )


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff_permission():
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


def test_faculty_sees_wallet_join_requests_with_details():
    faculty = _user(user_type=UserType.FACULTY, department=_internal_dept())
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    student = _user(user_type=UserType.STUDENT, name="Asha Verma")
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.PENDING
    )
    WalletJoinRequest.objects.create(
        student=_user(user_type=UserType.STUDENT), faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )

    item = _items(faculty)["wallet_join_requests"]
    assert item["count"] == 1
    assert item["link"] == "/student-management"
    assert "Asha Verma" in item["details"][0] and student.email in item["details"][0]

    res = _client_for(faculty).get("/api/notifications/pending-actions/")
    assert res.status_code == 200
    assert res.data["total"] >= 1


def test_booking_user_sees_payment_and_rating_items():
    student = _user(user_type=UserType.STUDENT)
    _booking(student, _equipment(), status=BookingStatus.PENDING_PAYMENT)
    _booking(student, _equipment(user_rating_enabled=True))
    _booking(student, _equipment(user_rating_enabled=False))

    items = _items(student)
    assert items["bookings_pending_payment"]["count"] == 1
    assert items["bookings_pending_payment"]["link"] == "/my-bookings"
    assert items["ratings_due"]["count"] == 1
    assert items["ratings_due"]["link"] == "/my-bookings?pending_rating=1"
    assert "Attention EQ" in items["ratings_due"]["details"][0]


def test_shared_data_counts_until_opened():
    dept = _internal_dept()
    owner = _user(user_type=UserType.STUDENT, department=dept, name="Owner Student")
    colleague = _user(user_type=UserType.FACULTY, department=dept)
    booking = _booking(owner, _equipment())
    BookingDataShare.objects.create(booking=booking, shared_by=owner, shared_with=colleague)

    item = _items(colleague)["shared_data_new"]
    assert item["count"] == 1
    assert item["link"] == "/shared-data"
    assert item["details"][0].startswith("Owner Student shared")

    BookingResultView.objects.create(booking=booking, user=colleague)
    assert "shared_data_new" not in _items(colleague)


def test_failing_section_does_not_hide_other_items():
    student = _user(user_type=UserType.STUDENT)
    _booking(student, _equipment(), status=BookingStatus.PENDING_PAYMENT)
    with patch(
        "iic_booking.equipment.results_sharing_service.is_internal_iitr_user", side_effect=RuntimeError("boom")
    ):
        items = _items(student)
    assert items["bookings_pending_payment"]["count"] == 1


def test_credit_clarification_notifies_user_and_shows_as_pending(django_capture_on_commit_callbacks):
    admin = _user(user_type=UserType.ADMIN)
    faculty = _user(user_type=UserType.FACULTY, department=_internal_dept())
    facility = WalletCreditFacility.objects.create(
        public_reference=f"WCF-{uuid.uuid4().hex[:6]}",
        user=faculty,
        requested_amount=Decimal("5000.00"),
        purpose="Consumables",
        status=WalletCreditFacilityStatus.SUBMITTED,
        submitted_at=timezone.now(),
    )
    assert _items(admin)["wallet_credit_requests"]["count"] == 1

    with django_capture_on_commit_callbacks(execute=True):
        return_for_clarification(facility=facility, actor=admin, reason="Attach the grant letter")

    rows = _bell(faculty)
    assert len(rows) == 1
    assert "clarification" in rows[0].subject.lower()
    assert "Attach the grant letter" in rows[0].message
    assert rows[0].metadata["link"] == "/wallet/credit-facility"
    assert _items(faculty)["wallet_credit_clarification"]["count"] == 1
    assert "wallet_credit_requests" not in _items(admin)


def test_nomination_flow_notifies_student_and_oic(staff_permission, django_capture_on_commit_callbacks):
    dept = _internal_dept()
    faculty = _user(user_type=UserType.FACULTY, department=dept, name="Dr. Supervisor")
    student = _user(user_type=UserType.STUDENT, department=dept, supervisor=faculty)
    oic = _user(user_type=UserType.MANAGER)
    eq = _equipment(name="XRD")
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    today = date.today()
    semester = Semester.objects.create(
        name="Test sem", code=f"T-{uuid.uuid4().hex[:6]}", start_date=today, end_date=today + timedelta(days=120)
    )

    with django_capture_on_commit_callbacks(execute=True):
        res = _client_for(faculty).post(
            "/api/equipment-nominations/",
            {"student_id": student.id, "equipment_id": eq.equipment_id, "semester_id": semester.id},
            format="json",
        )
    assert res.status_code == 201, res.data
    rows = _bell(student)
    assert len(rows) == 1
    assert rows[0].metadata["link"] == "/my-nomination-requests"
    assert "Dr. Supervisor" in rows[0].message and "XRD" in rows[0].message
    assert _items(student)["nomination_resume"]["count"] == 1

    nom = StudentEquipmentNomination.objects.get(student=student)
    nom.resume_submitted_at = timezone.now()
    nom.save(update_fields=["resume_submitted_at"])
    assert "nomination_resume" not in _items(student)
    oic_item = _items(oic)["nominations_to_review"]
    assert oic_item["count"] == 1
    assert oic_item["link"] == "/ta-nominations-log"

    with django_capture_on_commit_callbacks(execute=True):
        ok = _client_for(oic).post(f"/api/equipment-nominations/{nom.id}/approve/", {}, format="json")
    assert ok.status_code == 200, ok.data
    assert [r.subject for r in _bell(student)][-1] == "Equipment operating nomination approved"
    assert _bell(faculty)[-1].metadata["link"] == "/student-management"
    assert "nominations_to_review" not in _items(oic)
