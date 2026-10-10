"""Dashboard insight pages: equipment, users and cancellations — access, scope, definitions and query counts."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.booking_cancellation_log import record_cancellation
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingStatus,
    CancellationActorRole,
    CancellationReason,
    DisruptionEvent,
    EquipmentManager,
)
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

EQUIPMENT = "/api/admin/insights/equipment/"
USERS = "/api/admin/insights/users/"
CANCELLATIONS = "/api/admin/insights/cancellations/"


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
    """An approved (active) account; ``is_active`` follows ``admin_approved`` on save."""
    fields.setdefault("admin_approved", True)
    return UserFactory(**fields)


def _admin():
    return _person(user_type=UserType.ADMIN)


def _dept_admin(department):
    return _person(user_type=UserType.DEPT_ADMIN, department=department)


@pytest.mark.parametrize("url", [EQUIPMENT, USERS, CANCELLATIONS])
@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.FACULTY, UserType.MANAGER, UserType.OPERATOR])
def test_only_dashboard_viewers_have_access(url, user_type):
    assert _client(UserFactory(user_type=user_type)).get(url).status_code == 403


@pytest.mark.parametrize("url", [EQUIPMENT, USERS, CANCELLATIONS])
def test_anonymous_is_rejected(url):
    assert APIClient().get(url).status_code in (401, 403)


# --------------------------------------------------------------------------- equipment


@pytest.fixture
def labs():
    from iic_booking.equipment.models import Equipment

    Equipment.objects.all().delete()  # migration 0148 seeds a sample 3D printer
    a, b = _EgsFactory(), _EgsFactory()
    eq = {
        "active": a.equipment(),
        "repair": a.equipment(status="REPAIR"),
        "disposed": a.equipment(status="DISPOSED"),
        "none": a.equipment(status=None),
        "b": b.equipment(),
    }
    return a, b, eq


def test_equipment_totals_match_the_card(labs):
    _a, _b, eq = labs
    data = _get(_admin(), EQUIPMENT)
    card = data["card"]
    assert card == {"total": 4, "operational": 2, "under_maintenance": 1, "disposed": 1, "other": 1}
    assert data["summary"]["total"] == card["total"] == data["count"]
    groups = {g["key"]: g["count"] for g in data["summary"]["by_status"]}
    assert groups == {"operational": 2, "under_maintenance": 1, "other": 1}
    assert eq["disposed"].pk not in {r["equipment_id"] for r in data["results"]}

    disposed = _get(_admin(), EQUIPMENT, status="disposed")
    assert [r["equipment_id"] for r in disposed["results"]] == [eq["disposed"].pk]
    maintenance = _get(_admin(), EQUIPMENT, status="under_maintenance")
    assert [r["equipment_id"] for r in maintenance["results"]] == [eq["repair"].pk]


def test_equipment_department_admin_scope(labs):
    a, _b, eq = labs
    data = _get(_dept_admin(a.department), EQUIPMENT)
    assert data["scope"] == "department"
    assert data["card"]["total"] == data["summary"]["total"] == 3
    assert eq["b"].pk not in {r["equipment_id"] for r in data["results"]}


def test_equipment_row_details(labs):
    a, _b, eq = labs
    active, repair = eq["active"], eq["repair"]
    oic = UserFactory(user_type=UserType.MANAGER, name="Ravi Kumar")
    EquipmentManager.objects.create(equipment=active, manager=oic)
    now = timezone.now()
    DisruptionEvent.objects.create(
        equipment=repair, disruption_type="UNDER_MAINTENANCE", scope="EQUIPMENT", start_at=now - timedelta(hours=5)
    )
    student = a.student()
    past = timezone.localtime(now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    a.booking(student, active, past, total_charge="50.00")
    a.slot(active, past + timedelta(hours=2))
    a.booking(student, active, a.future(days=2), total_charge="50.00")
    tester = a.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    a.booking(tester, active, a.future(days=3))

    data = _get(_admin(), EQUIPMENT, oic=str(oic.pk))
    assert [r["equipment_id"] for r in data["results"]] == [active.pk]
    row = data["results"][0]
    assert row["officers_in_charge"][0]["id"] == oic.pk
    assert row["upcoming_bookings"] == 1
    assert row["utilisation"] == pytest.approx(0.5)

    rows = {r["equipment_id"]: r for r in _get(_admin(), EQUIPMENT)["results"]}
    assert rows[repair.pk]["down_since"] is not None
    assert rows[repair.pk]["downtime_hours"] == pytest.approx(5, abs=0.2)
    assert rows[active.pk]["down_since"] is None
    oics = {o["label"]: o["count"] for o in _get(_admin(), EQUIPMENT)["summary"]["by_oic"]}
    assert oics["Ravi Kumar"] == 1


def test_equipment_search_sort_and_options(labs):
    _a, _b, eq = labs
    data = _get(_admin(), EQUIPMENT, search=eq["b"].code, with_options="1")
    assert [r["equipment_id"] for r in data["results"]] == [eq["b"].pk]
    assert {o["value"] for o in data["options"]["statuses"]} >= {"operational", "under_maintenance"}
    paged = _get(_admin(), EQUIPMENT, page_size=2, page=2, sort="-name")
    assert paged["page"] == 2 and len(paged["results"]) == 2 and paged["total_pages"] == 2


def test_equipment_query_count_is_constant(labs, django_assert_max_num_queries):
    from iic_booking.equipment.admin_insights.equipment import build_equipment_insights

    a, _b, _eq = labs
    for _ in range(6):
        a.equipment()
    admin = _admin()
    with django_assert_max_num_queries(8):
        build_equipment_insights(admin, {})


# --------------------------------------------------------------------------- users


@pytest.fixture
def people():
    a, b = _EgsFactory(), _EgsFactory()
    from iic_booking.users.models import Department

    org = Department.objects.create(
        name="Acme Steels", code="ACME01", department_type="external", external_subcategory="industries",
        state="uttarakhand",
    )
    users = {
        "faculty": _person(user_type=UserType.FACULTY, department=a.department),
        "btech": _person(user_type=UserType.STUDENT, department=a.department, degree_name="B.Tech. Civil"),
        "phd": _person(user_type=UserType.STUDENT, department=a.department, degree_name="Ph.D."),
        "postdoc": _person(
            user_type=UserType.STUDENT, department=a.department, user_type_alias="IITR Post Doctoral Fellows"
        ),
        "oic": _person(user_type=UserType.MANAGER, department=a.department),
        "industry": _person(user_type=UserType.INSTITUTE, department=org),
        "rnd": _person(user_type=UserType.RND, department=org),
        "b_student": _person(user_type=UserType.STUDENT, department=b.department, degree_name="M.Tech"),
        "inactive": _person(user_type=UserType.STUDENT, department=a.department, admin_approved=False),
        "tester": _person(user_type=UserType.STUDENT, department=a.department, is_test_account=True),
    }
    return a, b, org, users


def test_users_total_matches_the_card(people):
    admin = _admin()
    data = _get(admin, USERS)
    assert data["summary"]["total"] == data["card"]["active"] == data["count"]
    ids = {r["id"] for r in _get(admin, USERS, page_size=500)["results"]}
    assert people[3]["tester"].pk not in ids
    assert people[3]["inactive"].pk not in ids
    everyone = _get(admin, USERS, status="all")
    assert everyone["summary"]["inactive"] == 1


def test_users_categories_and_programmes(people):
    a, _b, org, users = people
    data = _get(_dept_admin(a.department), USERS)
    assert data["scope"] == "department"
    categories = {c["key"]: c["count"] for c in data["summary"]["by_category"]}
    assert categories["iitr_faculty"] == 1
    assert categories["iitr_student"] == 3
    assert categories["iitr_staff"] == 2  # the OIC and the department administrator
    programmes = {p["key"]: p["count"] for p in data["summary"]["by_programme"]}
    assert (programmes["ug"], programmes["phd"], programmes["postdoc"]) == (1, 1, 1)
    department = data["summary"]["internal_by_department"][0]
    assert (department["faculty"], department["students"], department["staff"]) == (1, 3, 2)

    institute = _get(_admin(), USERS, segment="external")
    assert {r["id"] for r in institute["results"]} == {users["industry"].pk, users["rnd"].pk}
    organisation = institute["summary"]["external_by_organisation"][0]
    assert (organisation["name"], organisation["count"], organisation["state"]) == ("Acme Steels", 2, "Uttarakhand")

    ug = _get(_admin(), USERS, programme="ug")
    assert {r["id"] for r in ug["results"]} == {users["btech"].pk}
    assert ug["results"][0]["programme_display"] == "Undergraduate"


def test_degree_classification_table_wins(people):
    from iic_booking.users.models.channel_i_identity import StudentDegreeClassification

    users = people[3]
    StudentDegreeClassification.objects.create(
        channel_i_degree_name="B.Tech. Civil", channel_i_degree_name_normalized="b.tech. civil",
        classification="POSTGRADUATE",
    )
    pg = _get(_admin(), USERS, programme="pg")
    assert {r["id"] for r in pg["results"]} == {users["btech"].pk, users["b_student"].pk}


def test_users_booking_columns_and_period_filter(people):
    a, _b, _org, users = people
    eq = a.equipment()
    a.booking(users["btech"], eq, a.future(days=2))
    a.booking(users["btech"], eq, a.future(days=3))
    today = timezone.localdate().isoformat()
    data = _get(_admin(), USERS, booked_from=today, booked_to=today)
    assert [r["id"] for r in data["results"]] == [users["btech"].pk]
    assert data["results"][0]["bookings_count"] == 2
    assert data["results"][0]["last_booking_at"]
    by_bookings = _get(_admin(), USERS, sort="-bookings")
    assert by_bookings["results"][0]["id"] == users["btech"].pk


def test_wallet_link_only_for_main_admin(people):
    from iic_booking.users.models.wallet import Wallet

    a, _b, _org, users = people
    Wallet.objects.get_or_create(user=users["faculty"])
    Wallet.objects.filter(user=users["btech"]).delete()
    users["btech"].supervisor = users["faculty"]
    users["btech"].save(update_fields=["supervisor"])
    rows = {r["id"]: r for r in _get(_admin(), USERS, page_size=500)["results"]}
    assert rows[users["faculty"].pk]["wallet_owner_id"] == users["faculty"].pk
    assert rows[users["btech"].pk]["wallet_owner_id"] == users["faculty"].pk
    dept_rows = _get(_dept_admin(a.department), USERS, page_size=500)["results"]
    assert all(r["wallet_owner_id"] is None for r in dept_rows)


def test_users_trend_and_search(people):
    users = people[3]
    data = _get(_admin(), USERS, trend="week", search=users["faculty"].email)
    assert [r["id"] for r in data["results"]] == [users["faculty"].pk]
    trend = data["summary"]["trend"]
    assert trend["granularity"] == "week" and len(trend["series"]) == 16
    assert sum(p["total"] for p in trend["series"]) == 1


def test_users_query_count_is_constant(people, django_assert_max_num_queries):
    from iic_booking.equipment.admin_insights.users import build_user_insights

    for _ in range(8):
        UserFactory(user_type=UserType.STUDENT, department=people[0].department)
    admin = _admin()
    with django_assert_max_num_queries(24):
        build_user_insights(admin, {})


# --------------------------------------------------------------------------- cancellations


@pytest.fixture
def cancelled():
    """Department A: 3 bookings, 2 cancelled (one late); department B: 1 booking cancelled; one test account."""
    a, b = _EgsFactory(), _EgsFactory()
    eq_a, eq_b = a.equipment(), b.equipment()
    student_a, student_b = a.student(), b.student()
    admin = _admin()
    now = timezone.now()

    def cancel(booking, *, actor, role_system=False, minutes_before=3 * 24 * 60, status="CANCELLED", reason=None):
        booking.status = status
        booking.save(update_fields=["status"])
        slot_ids = list(booking.daily_slots.values_list("id", flat=True))
        booking.daily_slots.update(booking=None, status="AVAILABLE")
        row = record_cancellation(
            booking,
            previous_status="BOOKED",
            new_status=status,
            actor=actor,
            system=role_system,
            reason=reason,
            refund_amount=Decimal("0.00") if status == "CANCELLED" else None,
            released_slot_ids=slot_ids,
            cancelled_at=now,
        )
        return row

    early = a.booking(student_a, eq_a, a.future(days=3), total_charge="100.00")
    late = a.booking(student_a, eq_a, now + timedelta(hours=5), total_charge="40.00")
    a.booking(student_a, eq_a, a.future(days=6), total_charge="10.00")
    other = b.booking(student_b, eq_b, b.future(days=4), total_charge="70.00")
    rows = {
        "early": cancel(early, actor=student_a),
        "late": cancel(late, actor=admin, status="REFUNDED"),
        "other": cancel(other, actor=None, role_system=True),
    }
    tester = a.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    cancel(a.booking(tester, eq_a, a.future(days=2)), actor=tester)
    no_show = a.booking(student_a, eq_a, a.future(days=8), total_charge="5.00")
    cancel(no_show, actor=None, role_system=True, status="BOOKING_NOT_UTILIZED")
    return a, b, eq_a, rows


def test_cancellation_counts_rate_and_breakdowns(cancelled):
    data = _get(_admin(), CANCELLATIONS)
    summary = data["summary"]
    assert summary["total"] == data["count"] == 3
    assert summary["late"] == 1
    assert summary["bookings_created"] == 5
    assert summary["rate"] == pytest.approx(3 / 5)
    assert summary["refund_total"] == pytest.approx(40.0)
    assert summary["refund_estimated"] == 1
    roles = {r["key"]: r["count"] for r in summary["by_role"]}
    assert roles == {
        CancellationActorRole.USER: 1, CancellationActorRole.MAIN_ADMIN: 1, CancellationActorRole.SYSTEM: 1
    }
    reasons = {r["key"]: r["count"] for r in summary["by_reason"]}
    assert reasons[CancellationReason.USER_REQUEST] == 1
    assert len(summary["trend"]["series"]) == 30
    assert summary["previous"]["total"] == 0

    with_no_shows = _get(_admin(), CANCELLATIONS, include_no_shows="1")
    assert with_no_shows["summary"]["total"] == 4


def test_cancellation_rows_link_bookings(cancelled):
    _a, _b, _eq, rows = cancelled
    data = _get(_admin(), CANCELLATIONS, late_only="1")
    assert [r["id"] for r in data["results"]] == [rows["late"].pk]
    row = data["results"][0]
    booking = rows["late"].booking
    assert row["booking"] == {
        "pk": booking.pk,
        "display_id": booking.virtual_booking_id,
        "status": "REFUNDED",
        "status_display": booking.get_status_display(),
    }
    assert row["late"] is True and row["actor_role"] == CancellationActorRole.MAIN_ADMIN


def test_cancellation_filters(cancelled):
    _a, _b, eq_a, rows = cancelled
    assert _get(_admin(), CANCELLATIONS, role="SYSTEM")["count"] == 1
    assert _get(_admin(), CANCELLATIONS, equipment=str(eq_a.pk))["count"] == 2
    assert _get(_admin(), CANCELLATIONS, category="iitr_student")["count"] == 3
    assert _get(_admin(), CANCELLATIONS, reason="USER_REQUEST")["count"] == 1
    old = (timezone.localdate() - timedelta(days=40)).isoformat()
    assert _get(_admin(), CANCELLATIONS, date_from=old, date_to=old)["count"] == 0


def test_cancellation_department_scope(cancelled):
    a, _b, _eq, rows = cancelled
    data = _get(_dept_admin(a.department), CANCELLATIONS)
    assert {r["id"] for r in data["results"]} == {rows["early"].pk, rows["late"].pk}
    assert data["summary"]["bookings_created"] == 4


def test_refilled_slots(cancelled):
    a, _b, eq_a, rows = cancelled
    released = rows["early"].released_slot_ids
    from iic_booking.equipment.models import DailySlot

    taker = a.booking(a.student(), eq_a, a.future(days=20))
    DailySlot.objects.filter(pk__in=released).update(booking=taker, status="BOOKED")
    BookingEvent.objects.create(
        booking=taker, event_type=BookingEventType.CREATED, new_status=BookingStatus.BOOKED,
        metadata={"from_waitlist": True},
    )
    data = _get(_admin(), CANCELLATIONS)
    refill = {r["id"]: r["refill"] for r in data["results"]}
    assert refill[rows["early"].pk] == "waitlist"
    assert refill[rows["late"].pk] == "not_refilled"
    counts = {r["key"]: r["count"] for r in data["summary"]["refills"]}
    assert counts["waitlist"] == 1


def test_dashboard_card_counts_cancellations(cancelled):
    data = _get(_admin(), "/api/admin/dashboard-summary/", refresh="1")
    card = data["cancellations"]
    assert (card["total"], card["late"], card["days"]) == (3, 1, 30)
    assert card["rate"] == pytest.approx(3 / 5)


def test_cancellation_query_count_is_constant(cancelled, django_assert_max_num_queries):
    from iic_booking.equipment.admin_insights.cancellations import build_cancellation_insights

    admin = _admin()
    with django_assert_max_num_queries(25):
        build_cancellation_insights(admin, {})


@pytest.mark.parametrize(
    "report", ["admin-equipment-overview", "admin-users-overview", "admin-cancellations"]
)
def test_exports(cancelled, report):
    resp = _client(_admin()).get(f"/api/exports/{report}/", {"export_format": "csv"})
    assert resp.status_code == 200, resp.content
    student = UserFactory(user_type=UserType.STUDENT)
    assert _client(student).get(f"/api/exports/{report}/", {"export_format": "csv"}).status_code == 403
