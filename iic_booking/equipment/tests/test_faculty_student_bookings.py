"""Searching or filtering bookings by an IITR faculty member also lists their approved wallet students' bookings."""

from __future__ import annotations

import pytest

from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def lab(egs_factory):
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    equipment = egs_factory.equipment()
    faculty = UserFactory(user_type=UserType.FACULTY, name="Prof Kavita Rao", admin_approved=True)
    wallet = Wallet.objects.create(user=faculty)
    joined = egs_factory.student()
    pending = egs_factory.student()
    stranger = egs_factory.student()
    WalletJoinRequest.objects.create(
        student=joined, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    WalletJoinRequest.objects.create(
        student=pending, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.PENDING
    )
    bookings = {
        "faculty": egs_factory.booking(faculty, equipment, egs_factory.future(days=3, hour=9)),
        "joined": egs_factory.booking(joined, equipment, egs_factory.future(days=3, hour=10)),
        "pending": egs_factory.booking(pending, equipment, egs_factory.future(days=3, hour=11)),
        "stranger": egs_factory.booking(stranger, equipment, egs_factory.future(days=3, hour=12)),
    }
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True)
    return egs_factory, faculty, bookings, admin


def _listed(factory, viewer, query):
    resp = factory.client_for(viewer).get(f"/api/bookings/?{query}&limit=100")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    rows = body["bookings"] if isinstance(body, dict) else body
    return {row.get("real_booking_id") or row.get("booking_id") for row in rows}


def _ids(bookings, *keys):
    return {bookings[k].pk for k in keys}


def test_search_by_faculty_email_includes_approved_students(lab):
    factory, faculty, bookings, admin = lab
    assert _listed(factory, admin, f"search={faculty.email}") == _ids(bookings, "faculty", "joined")


def test_search_by_faculty_name_includes_approved_students(lab):
    factory, faculty, bookings, admin = lab
    assert _listed(factory, admin, "search=Kavita") == _ids(bookings, "faculty", "joined")


def test_user_filter_on_faculty_includes_approved_students(lab):
    factory, faculty, bookings, admin = lab
    assert _listed(factory, admin, f"user_id={faculty.pk}") == _ids(bookings, "faculty", "joined")


def test_user_filter_on_a_student_lists_only_that_student(lab):
    factory, faculty, bookings, admin = lab
    student = bookings["joined"].user
    assert _listed(factory, admin, f"user_id={student.pk}") == _ids(bookings, "joined")


def test_faculty_sees_own_and_approved_students_bookings(lab):
    factory, faculty, bookings, admin = lab
    assert _listed(factory, faculty, "status=BOOKED") == _ids(bookings, "faculty", "joined")
