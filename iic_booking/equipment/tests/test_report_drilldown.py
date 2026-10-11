"""Reports & Statistics drill-downs: every level adds up to the row it was opened from, within the viewer's scope."""

import csv
import io
from datetime import timedelta
from types import SimpleNamespace

import pytest

from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

PERF = "/api/admin/equipment-reports/"
FIN = "/api/finance/reports/"


def _done(f, user, eq, start, charge, snapshot):
    booking = f.booking(user, eq, start, total_charge=charge)
    booking.status = BookingStatus.COMPLETED
    booking.completed_at = start + timedelta(hours=1)
    booking.user_type_snapshot = snapshot
    booking.save(update_fields=["status", "completed_at", "user_type_snapshot"])
    return booking


@pytest.fixture
def world(egs_factory):
    f = egs_factory
    eq_a = f.equipment(name="Alpha XPS")
    eq_b = f.equipment(name="Beta SEM")
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=f.department)
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, department=f.department)
    EquipmentManager.objects.create(equipment=eq_a, manager=oic)
    EquipmentOperator.objects.create(equipment=eq_a, operator=operator)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True, is_staff=True)
    finance = UserFactory(user_type=UserType.FINANCE, admin_approved=True, department=f.department)
    other_dept = Department.objects.create(name="Acme Industries", code="ACMEX")
    s1, s2 = f.student(), f.student()
    fac = UserFactory(user_type=UserType.FACULTY, department=f.department)
    ind = UserFactory(user_type=UserType.INSTITUTE, department=other_dept)
    tester = UserFactory(user_type=UserType.STUDENT, department=f.department, is_test_account=True)
    outsider = f.student()
    start = f.future(days=3, hour=8)
    hour = timedelta(hours=1)
    bookings = [
        _done(f, s1, eq_a, start, "100.00", UserType.STUDENT),
        _done(f, s1, eq_b, start + hour, "50.00", UserType.STUDENT),
        _done(f, s2, eq_a, start + 2 * hour, "75.50", UserType.INDIVIDUAL_STUDENT),
        _done(f, fac, eq_a, start + 3 * hour, "200.00", UserType.FACULTY),
        _done(f, ind, eq_b, start + 4 * hour, "900.00", "industry"),
        _done(f, tester, eq_a, start + 5 * hour, "999.00", UserType.STUDENT),
    ]
    period = {
        "date_from": (start.date() - timedelta(days=1)).isoformat(),
        "date_to": (start.date() + timedelta(days=1)).isoformat(),
    }
    return SimpleNamespace(f=f, eq_a=eq_a, eq_b=eq_b, oic=oic, operator=operator, admin=admin, finance=finance,
                           s1=s1, s2=s2, fac=fac, ind=ind, tester=tester, outsider=outsider, bookings=bookings,
                           period=period)


def _get(world, viewer, url, /, **params):
    res = world.f.client_for(viewer).get(url, {**world.period, **params})
    assert res.status_code == 200, getattr(res, "data", res.content[:300])
    return res.data


def _assert_reconciles(world, user, base, row_total, row_count, dimension, value):
    users = _get(world, user, f"{base}drilldown/", dimension=dimension, value=value)
    assert users["segment"]["revenue"] == pytest.approx(row_total)
    assert users["segment"]["bookings"] == row_count
    assert sum(u["revenue"] for u in users["users"]) == pytest.approx(row_total)
    assert sum(u["bookings"] for u in users["users"]) == row_count
    bookings = _get(world, user, f"{base}drilldown-bookings/", dimension=dimension, value=value)
    assert bookings["count"] == row_count
    assert sum(b["charge"] for b in bookings["bookings"]) == pytest.approx(row_total)
    for u in users["users"]:
        mine = _get(world, user, f"{base}drilldown-bookings/", dimension=dimension, value=value, user=u["user_id"])
        assert mine["user"]["bookings"] == u["bookings"] == mine["count"]
        assert sum(b["charge"] for b in mine["bookings"]) == pytest.approx(u["revenue"])
        assert {b["user"]["id"] for b in mine["bookings"]} == {u["user_id"]}
    return users


@pytest.mark.django_db
def test_user_type_rows_use_iitr_labels_and_merge_student_codes(world):
    fin = _get(world, world.admin, PERF)["financial"]
    by_label = {r["user_type_label"]: r for r in fin["revenue_by_user_type"]}
    assert by_label["IITR Students"]["total"] == pytest.approx(225.5)
    assert by_label["IITR Students"]["count"] == 3 and by_label["IITR Students"]["users"] == 2
    assert by_label["IITR Faculty"]["total"] == pytest.approx(200.0)
    assert by_label["External – Industry"]["total"] == pytest.approx(900.0)
    assert [r["user_type_label"] for r in fin["revenue_by_external_category"]] == ["External – Industry"]
    assert "student" not in by_label and "faculty" not in by_label


@pytest.mark.django_db
@pytest.mark.parametrize(
    "table, value_key",
    [
        ("revenue_by_user_type", "drill_value"),
        ("revenue_by_external_category", "drill_value"),
        ("revenue_by_department", "drill_value"),
        ("revenue_by_equipment", "drill_value"),
        ("revenue_by_month", "drill_value"),
    ],
)
def test_performance_drilldown_adds_up_to_every_row(world, table, value_key):
    dimension = {
        "revenue_by_user_type": "category",
        "revenue_by_external_category": "external_category",
        "revenue_by_department": "department",
        "revenue_by_equipment": "equipment",
        "revenue_by_month": "month",
    }[table]
    rows = _get(world, world.admin, PERF)["financial"][table]
    assert rows
    for row in rows:
        _assert_reconciles(world, world.admin, PERF, row["total"], row["count"], dimension, row[value_key])


@pytest.mark.django_db
def test_performance_totals_and_test_accounts_left_out(world):
    summary = _get(world, world.admin, PERF)["summary"]
    for dimension, key in (("all", "revenue_total"), ("internal", "revenue_internal"),
                           ("external", "revenue_external")):
        users = _get(world, world.admin, f"{PERF}drilldown/", dimension=dimension, value="")
        assert users["segment"]["revenue"] == pytest.approx(summary[key])
        assert world.tester.pk not in {u["user_id"] for u in users["users"]}
    everything = _get(world, world.admin, f"{PERF}drilldown/", dimension="all")
    assert everything["segment"]["revenue"] == pytest.approx(1325.5)


@pytest.mark.django_db
def test_user_rows_and_booking_links(world):
    users = _get(world, world.admin, f"{PERF}drilldown/", dimension="category", value="iitr_student")
    assert users["segment"]["label"] == "IITR Students"
    s1 = next(u for u in users["users"] if u["user_id"] == world.s1.pk)
    assert s1["category_label"] == "IITR Students" and s1["bookings"] == 2 and s1["revenue"] == 150.0
    mine = _get(world, world.admin, f"{PERF}drilldown-bookings/", dimension="category", value="iitr_student",
                user=world.s1.pk)
    assert {b["display_id"] for b in mine["bookings"]} == {
        world.bookings[0].virtual_booking_id, world.bookings[1].virtual_booking_id}
    assert {b["equipment"]["id"] for b in mine["bookings"]} == {world.eq_a.pk, world.eq_b.pk}


@pytest.mark.django_db
def test_oic_and_operator_drilldowns_stay_in_scope(world):
    oic_rows = _get(world, world.oic, PERF)["financial"]["revenue_by_user_type"]
    for row in oic_rows:
        _assert_reconciles(world, world.oic, PERF, row["total"], row["count"], "category", row["drill_value"])
    everything = _get(world, world.oic, f"{PERF}drilldown/", dimension="all")
    assert everything["segment"]["revenue"] == pytest.approx(375.5)
    assert world.ind.pk not in {u["user_id"] for u in everything["users"]}
    other = _get(world, world.oic, f"{PERF}drilldown/", dimension="all", equipment_id=world.eq_b.pk)
    assert other["users"] == []

    op = _get(world, world.operator, f"{PERF}drilldown/", dimension="all")
    assert op["revenue_visible"] is False and "revenue" not in op["segment"]
    assert all("revenue" not in u for u in op["users"]) and op["segment"]["bookings"] == 3
    op_bookings = _get(world, world.operator, f"{PERF}drilldown-bookings/", dimension="all")
    assert all("charge" not in b for b in op_bookings["bookings"])


@pytest.mark.django_db
def test_drilldown_rejects_students_and_bad_input(world):
    client = world.f.client_for(world.s1)
    assert client.get(f"{PERF}drilldown/", {"dimension": "all"}).status_code == 403
    assert client.get(f"{FIN}drilldown/", {"dimension": "all"}).status_code == 403
    admin = world.f.client_for(world.admin)
    assert admin.get(f"{PERF}drilldown/", {"dimension": "nope", "value": "x"}).status_code == 400
    assert admin.get(f"{PERF}drilldown/", {"dimension": "category"}).status_code == 400
    assert admin.get(f"{PERF}drilldown-bookings/", {"dimension": "all", "user": "abc"}).status_code == 400


@pytest.mark.django_db
def test_finance_drilldown_adds_up_to_every_row(world):
    report = _get(world, world.finance, f"{FIN}dashboard/")
    tables = {
        "equipment_department": report["charts"]["revenue_by_department"],
        "category": report["charts"]["revenue_by_org_category"],
        "equipment": report["charts"]["equipment_revenue"],
        "internal_department": report["tables"]["internal_by_department"],
        "internal_equipment": report["tables"]["internal_by_equipment"],
        "external_category": report["tables"]["external_by_category"],
    }
    for dimension, rows in tables.items():
        assert rows, dimension
        for row in rows:
            _assert_reconciles(world, world.finance, FIN, row["revenue"], row["bookings"], dimension,
                               row["drill_value"])
    labels = {r["name"] for r in report["charts"]["revenue_by_org_category"]}
    assert {"IITR Students", "IITR Faculty", "External – Industry"} <= labels
    summary = report["summary"]
    for dimension, key in (("all", "total_revenue"), ("internal", "internal_revenue"),
                           ("external", "external_revenue")):
        users = _get(world, world.finance, f"{FIN}drilldown/", dimension=dimension)
        assert users["segment"]["revenue"] == pytest.approx(summary[key])


@pytest.mark.django_db
def test_drilldown_exports_link_to_users_and_bookings(world):
    client = world.f.client_for(world.admin)
    res = client.get("/api/exports/report-drilldown-users/", {
        "export_format": "csv", "src": "performance", "dimension": "category", "value": "iitr_student",
        **world.period})
    assert res.status_code == 200, res.content[:300]
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert len(rows) - 1 == 2
    res = client.get("/api/exports/report-drilldown-bookings/", {
        "export_format": "csv", "src": "performance", "dimension": "category", "value": "iitr_student",
        "user": world.s1.pk, **world.period})
    assert res.status_code == 200, res.content[:300]
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert len(rows) - 1 == 2
    pdf = client.get("/api/exports/report-drilldown-bookings/", {
        "export_format": "pdf", "src": "performance", "dimension": "all", **world.period})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert world.f.client_for(world.s1).get("/api/exports/report-drilldown-users/", {
        "src": "performance", "dimension": "all"}).status_code == 403


@pytest.mark.django_db
def test_user_card_access_by_role(world):
    def card(viewer, person):
        return world.f.client_for(viewer).get(f"/api/users/{person.pk}/card/")

    own = card(world.s1, world.s1)
    assert own.status_code == 200 and own.data["access"] == "self"
    assert card(world.s1, world.s2).status_code == 404
    scoped = card(world.oic, world.s1)
    assert scoped.status_code == 200 and scoped.data["access"] == "scoped"
    assert scoped.data.get("wallet") is None
    assert card(world.oic, world.ind).status_code == 404
    assert card(world.oic, world.outsider).status_code == 404
    assert card(world.finance, world.ind).status_code == 200
    full = card(world.admin, world.ind)
    assert full.status_code == 200 and full.data["access"] == "full"
    assert world.f.client_for(world.admin).get("/api/users/999999999/card/").status_code == 404
