"""Refund requests on the Cancellations page and the user card on the Users overview."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.booking_cancellation_log import record_cancellation
from iic_booking.equipment.models import BookingCancellationRequest, BookingEvent, BookingEventType
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

REFUNDS = "/api/admin/insights/refund-requests/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _get(user, url, **params):
    resp = _client(user).get(url, params)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _person(**fields):
    fields.setdefault("admin_approved", True)
    return UserFactory(**fields)


def _admin():
    return _person(user_type=UserType.ADMIN)


def _credit(user, owner, department, booking, amount, *, partial=False):
    from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction, Wallet

    wallet, _ = Wallet.objects.get_or_create(user=owner)
    sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=department)
    prefix = "Partial refund for cancelled slot(s) on Booking " if partial else "Refund for cancelled Booking "
    return SubWalletTransaction.objects.create(
        sub_wallet=sub,
        transaction_type="credit",
        amount=Decimal(amount),
        description=f"{prefix}{booking.virtual_booking_id}- {booking.equipment.name}",
        related_user=user,
    )


@pytest.fixture
def refunds():
    """Student A: self-service refund (in window) + pending request; student B: partial refund inside the cut-off."""
    a, b = _EgsFactory(), _EgsFactory()
    eq_a = a.equipment(reschedule_hours_threshold=48)
    eq_b = b.equipment(reschedule_hours_threshold=48)
    student_a, student_b = a.student(), b.student()
    faculty = _person(user_type=UserType.FACULTY, department=a.department)
    now = timezone.now()

    full = a.booking(student_a, eq_a, a.future(days=4), total_charge="100.00")
    full.status = "REFUNDED"
    full.save(update_fields=["status"])
    record_cancellation(
        full, previous_status="BOOKED", new_status="REFUNDED", actor=student_a, refund_amount="100.00",
        cancelled_at=now,
    )
    txn = _credit(student_a, faculty, a.department, full, "100.00")

    asked = a.booking(student_a, eq_a, a.future(days=5), total_charge="60.00")
    request = BookingCancellationRequest.objects.create(booking=asked, user=student_a, notes="Change of plan")

    partial = b.booking(student_b, eq_b, now + timedelta(hours=20), total_charge="50.00", slot_count=2)
    slot = timezone.localtime(now + timedelta(hours=21)).strftime("%Y-%m-%d %H:%M")
    event = BookingEvent.objects.create(
        booking=partial, event_type=BookingEventType.STATUS_CHANGED, previous_status="BOOKED", new_status="BOOKED",
        comment=f"Partial cancellation by user: 1 slot(s) released ({slot}). ₹25.00 refunded to wallet.",
        created_by=student_b,
    )
    _credit(student_b, faculty, b.department, partial, "25.00", partial=True)

    by_admin = a.booking(student_a, eq_a, a.future(days=6), total_charge="10.00")
    by_admin.status = "CANCELLED"
    by_admin.save(update_fields=["status"])
    record_cancellation(by_admin, previous_status="BOOKED", new_status="CANCELLED", actor=_admin(), cancelled_at=now)

    tester = a.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    test_booking = a.booking(tester, eq_a, a.future(days=3))
    test_booking.status = "REFUNDED"
    test_booking.save(update_fields=["status"])
    record_cancellation(
        test_booking, previous_status="BOOKED", new_status="REFUNDED", actor=tester, refund_amount="10.00",
        cancelled_at=now,
    )
    return {"a": a, "b": b, "student_a": student_a, "student_b": student_b, "faculty": faculty, "full": full,
            "txn": txn,
            "request": request, "event": event, "partial": partial}


@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.MANAGER])
def test_refund_requests_need_dashboard_access(user_type):
    assert _client(UserFactory(user_type=user_type)).get(REFUNDS).status_code == 403


def test_refund_requests_kpis_and_rows(refunds):
    data = _get(_admin(), REFUNDS)
    summary = data["summary"]
    assert summary["total"] == data["count"] == 3
    assert summary["unique_users"] == 2
    assert summary["within_window"] == 2
    assert summary["unique_users_within_window"] == 1
    assert summary["repeat_refunders"] == 1
    assert summary["repeaters"][0]["user"]["id"] == refunds["student_a"].pk
    assert summary["repeaters"][0]["count"] == 2
    assert summary["refund_total"] == pytest.approx(125.0)
    assert summary["bookings_created"] == 4
    sources = {s["key"]: s["count"] for s in summary["by_source"]}
    assert sources == {"self_service": 1, "request": 1, "partial": 1}
    statuses = {s["key"]: s["count"] for s in summary["by_status"] if s["count"]}
    assert statuses == {"refunded": 2, "pending": 1}

    rows = {r["source"]: r for r in data["results"]}
    full = rows["self_service"]
    assert full["booking"]["pk"] == refunds["full"].pk
    assert full["within_window"] is True and full["window_hours"] == 48
    assert full["lead_minutes"] > 48 * 60
    assert full["refund"] == pytest.approx(100.0)
    assert full["wallet_transaction"]["id"] == refunds["txn"].pk
    assert full["wallet_transaction"]["wallet_owner_id"] == refunds["faculty"].pk
    pending = rows["request"]
    assert (pending["status"], pending["refund"], pending["wallet_transaction"]) == ("pending", None, None)
    assert pending["note"] == "Change of plan"
    partial = rows["partial"]
    assert partial["within_window"] is False
    assert partial["refund"] == pytest.approx(25.0)
    assert partial["wallet_transaction"]["amount"] == pytest.approx(25.0)


def test_refund_request_filters(refunds):
    admin = _admin()
    assert _get(admin, REFUNDS, window="outside")["count"] == 1
    assert _get(admin, REFUNDS, status="pending")["count"] == 1
    assert _get(admin, REFUNDS, source="partial")["count"] == 1
    assert _get(admin, f"{REFUNDS}?user={refunds['student_a'].pk}")["count"] == 2
    old = (timezone.localdate() - timedelta(days=40)).isoformat()
    assert _get(admin, REFUNDS, date_from=old, date_to=old)["count"] == 0
    dept = _get(_person(user_type=UserType.DEPT_ADMIN, department=refunds["b"].department), REFUNDS)
    assert [r["source"] for r in dept["results"]] == ["partial"]
    assert dept["results"][0]["wallet_transaction"]["wallet_owner_id"] is None


def test_refunds_after_the_slot_started_are_not_requests(refunds):
    a = refunds["a"]
    student = refunds["student_a"]
    late = a.booking(student, a.equipment(), timezone.now() - timedelta(hours=2))
    late.status = "REFUNDED"
    late.save(update_fields=["status"])
    record_cancellation(late, previous_status="BOOKED", new_status="REFUNDED", actor=student, refund_amount="5.00")
    assert _get(_admin(), REFUNDS)["count"] == 3


def test_dashboard_card_counts_refund_requests(refunds):
    card = _get(_admin(), "/api/admin/dashboard-summary/", refresh="1")["cancellations"]
    assert card["refund_requests"] == 3
    assert card["refund_users"] == 2
    assert card["refund_users_within_window"] == 1
    assert card["repeat_refunders"] == 1
    assert card["total"] == 2  # the self-service refund and the admin cancellation


def test_refund_request_query_count(refunds, django_assert_max_num_queries):
    from iic_booking.equipment.admin_insights.refund_requests import build_refund_request_insights

    admin = _admin()
    with django_assert_max_num_queries(25):
        build_refund_request_insights(admin, {})


# --------------------------------------------------------------------------- user card


@pytest.fixture
def family():
    """A faculty wallet owner with two linked students; one student booked twice, one cancelled."""
    from iic_booking.users.models.wallet import SubWallet, Wallet, WalletJoinRequest, WalletJoinRequestStatus

    a, b = _EgsFactory(), _EgsFactory()
    eq_a, eq_b = a.equipment(), b.equipment()
    faculty = _person(user_type=UserType.FACULTY, department=a.department, emp_id="F-100")
    s1 = _person(user_type=UserType.STUDENT, department=a.department, supervisor=faculty, emp_id="S-1")
    s2 = _person(user_type=UserType.STUDENT, department=a.department, supervisor=faculty, emp_id="S-2")
    outsider = _person(user_type=UserType.STUDENT, department=b.department)
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    SubWallet.objects.create(wallet=wallet, department=a.department, balance=Decimal("500.00"))
    for s in (s1, s2):
        WalletJoinRequest.objects.create(
            faculty=faculty, student=s, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
        )
    bookings = {
        "f": a.booking(faculty, eq_a, a.future(days=2), total_charge="30.00"),
        "s1a": a.booking(s1, eq_a, a.future(days=3), total_charge="20.00"),
        "s1b": b.booking(s1, eq_b, b.future(days=4), total_charge="15.00"),
        "s2": a.booking(s2, eq_a, a.future(days=5), total_charge="40.00"),
        "out": b.booking(outsider, eq_b, b.future(days=2), total_charge="99.00"),
    }
    bookings["s2"].status = "CANCELLED"
    bookings["s2"].save(update_fields=["status"])
    return {"a": a, "b": b, "eq_a": eq_a, "faculty": faculty, "s1": s1, "s2": s2, "outsider": outsider,
            "bookings": bookings}


def _card(user, target):
    return _get(user, f"/api/admin/insights/users/{target.pk}/")


def test_user_card_profile_and_recent_bookings(family):
    data = _card(_admin(), family["s1"])
    profile = data["profile"]
    assert profile["employee_id"] == "S-1"
    assert profile["supervisor"]["id"] == family["faculty"].pk
    assert profile["category"] == "iitr_student"
    assert profile["department"]["id"] == family["a"].department.pk
    assert "last_login" in profile and "profile_picture_url" in profile
    assert data["bookings"]["total"] == 2
    assert data["bookings"]["charged"] == pytest.approx(35.0)
    assert {b["pk"] for b in data["bookings"]["recent"]} == {family["bookings"]["s1a"].pk, family["bookings"]["s1b"].pk}
    assert data["bookings"]["recent"][0]["slot_start"]
    assert data["wallet"] == {"owner_id": family["faculty"].pk, "is_owner": False, "balance": 500.0}
    assert data["linked_wallet"] is None

    owner = _card(_admin(), family["faculty"])
    assert owner["wallet"]["is_owner"] is True
    assert owner["linked_wallet"] == {"owner_id": family["faculty"].pk, "linked_users": 3}


def test_user_card_department_scope(family):
    dept_admin = _person(user_type=UserType.DEPT_ADMIN, department=family["a"].department)
    data = _card(dept_admin, family["s1"])
    assert data["wallet"] is None
    assert data["bookings"]["total"] == 1  # only bookings on the department's equipment
    assert _client(dept_admin).get(f"/api/admin/insights/users/{family['outsider'].pk}/").status_code == 404
    assert _client(UserFactory(user_type=UserType.STUDENT)).get(
        f"/api/admin/insights/users/{family['s1'].pk}/"
    ).status_code == 403


def test_supervisor_from_another_department_is_visible(family):
    other = _person(user_type=UserType.FACULTY, department=family["b"].department)
    family["s2"].supervisor = other
    family["s2"].save(update_fields=["supervisor"])
    dept_admin = _person(user_type=UserType.DEPT_ADMIN, department=family["a"].department)
    assert _card(dept_admin, other)["profile"]["id"] == other.pk


def test_wallet_bookings_by_linked_users(family):
    url = f"/api/admin/insights/users/{family['faculty'].pk}/wallet-bookings/"
    data = _get(_admin(), url, with_options="1")
    assert data["summary"]["linked_users"] == 3
    assert data["count"] == 4
    assert family["bookings"]["out"].pk not in {r["pk"] for r in data["results"]}
    members = {m["id"]: m for m in data["summary"]["by_member"]}
    assert members[family["s1"].pk]["bookings"] == 2
    assert members[family["s1"].pk]["charged"] == pytest.approx(35.0)
    assert members[family["s2"].pk]["cancelled"] == 1
    assert members[family["s2"].pk]["charged"] == 0
    assert data["summary"]["charged"] == pytest.approx(65.0)
    assert [m["is_owner"] for m in data["options"]["members"]][0] is True

    only_s1 = _get(_admin(), url, member=str(family["s1"].pk))
    assert only_s1["count"] == 2
    by_equipment = _get(_admin(), url, equipment=str(family["eq_a"].pk), status="BOOKED")
    assert by_equipment["count"] == 2

    student_url = f"/api/admin/insights/users/{family['s1'].pk}/wallet-bookings/"
    assert _client(_admin()).get(student_url).status_code == 404


def test_user_card_query_counts(family, django_assert_max_num_queries):
    from iic_booking.equipment.admin_insights.user_card import build_user_card, build_wallet_bookings

    admin = _admin()
    with django_assert_max_num_queries(20):
        build_user_card(admin, family["faculty"].pk)
    with django_assert_max_num_queries(20):
        build_wallet_bookings(admin, family["faculty"].pk, {"with_options": "1"})


@pytest.mark.parametrize("report,query", [
    ("admin-refund-requests", {}),
    ("admin-wallet-linked-bookings", None),
])
def test_exports(refunds, family, report, query):
    params = {"export_format": "csv", **(query if query is not None else {"owner": family["faculty"].pk})}
    resp = _client(_admin()).get(f"/api/exports/{report}/", params)
    assert resp.status_code == 200, resp.content
    student = UserFactory(user_type=UserType.STUDENT)
    assert _client(student).get(f"/api/exports/{report}/", params).status_code == 403
