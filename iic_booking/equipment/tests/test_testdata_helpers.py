"""Shared test-data definition: helpers and the flag_test_data command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command

from iic_booking.equipment import testdata
from iic_booking.equipment.models import Booking, Equipment, EquipmentCategory
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.equipment.testdata_models import TestDataFlag, TestDataKind
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def lab():
    Equipment.objects.all().delete()
    f = _EgsFactory()
    real = f.equipment(name="FE-SEM", code="SEM01")
    hidden = f.equipment(name="Hidden rig", code="HID01", visible_to_test_accounts_only=True)
    marked = f.equipment(name="Sample 3D Printer (TEST)", code="P3D")
    mode = f.equipment(name="Mode of marked", code="P3D-M", parent_equipment=marked)
    category = EquipmentCategory.objects.create(name="Test category", code="TESTCAT")
    in_category = f.equipment(name="Rig in test category", code="CAT01", category=category)
    student, tester = f.student(), f.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    testdata.mark(TestDataKind.EQUIPMENT, marked, reason="test")
    testdata.mark(TestDataKind.CATEGORY, category, reason="test")
    bookings = {
        "real": f.booking(student, real, f.future(days=2)),
        "by_tester": f.booking(tester, real, f.future(days=3)),
        "on_marked": f.booking(student, marked, f.future(days=4)),
        "on_hidden": f.booking(student, hidden, f.future(days=5)),
    }
    return {"real": real, "hidden": hidden, "marked": marked, "mode": mode, "category": category,
            "in_category": in_category, "student": student, "tester": tester, "bookings": bookings}


def test_test_equipment_definition(lab):
    ids = testdata.get_test_equipment_ids()
    assert ids == {lab[k].equipment_id for k in ("hidden", "marked", "mode", "in_category")}
    kept = testdata.exclude_test_equipment(Equipment.objects.all())
    assert list(kept.values_list("equipment_id", flat=True)) == [lab["real"].equipment_id]
    assert testdata.is_test_equipment(lab["mode"]) and not testdata.is_test_equipment(lab["real"])


def test_bookings_users_and_categories(lab):
    kept = testdata.exclude_test_bookings(Booking.objects.all())
    assert list(kept.values_list("pk", flat=True)) == [lab["bookings"]["real"].pk]
    users = testdata.exclude_test_users(Booking.objects.all(), "user__")
    assert lab["bookings"]["by_tester"].pk not in set(users.values_list("pk", flat=True))
    assert not testdata.exclude_test_categories(EquipmentCategory.objects.filter(pk=lab["category"].pk)).exists()


def test_marks_are_cached_and_refreshed(lab, django_assert_num_queries):
    testdata.marked_ids()
    with django_assert_num_queries(0):
        testdata.marked_ids()
    testdata.unmark(TestDataKind.EQUIPMENT, lab["marked"])
    assert lab["marked"].equipment_id not in testdata.marked_ids()["equipment"]


def _dry() -> str:
    buf = StringIO()
    call_command("flag_test_data", stdout=buf)
    return buf.getvalue()


def test_flag_command_dry_run_then_apply(lab):
    oic = UserFactory(user_type=UserType.MANAGER, name="Test Officer In Charge", email="oic@iic.example")
    op = UserFactory(user_type=UserType.OPERATOR, name="Lab person", email="test.operator@iitr.ac.in")
    fake = UserFactory(user_type=UserType.STUDENT, name="Real Name", email="someone@ic-booking.test")
    keep = UserFactory(user_type=UserType.FACULTY, name="Prof. Testa", email="testa@iitr.ac.in")
    laser = _EgsFactory().equipment(name="Laser cutter", code="LC1")
    tensile = _EgsFactory().equipment(name="Tensile tester", code="UTM")
    iictest = _EgsFactory().equipment(name="Demo rig", code="IICTEST-01")

    dry = StringIO()
    call_command("flag_test_data", "--equipment-ids", str(laser.equipment_id), stdout=dry)
    text = dry.getvalue()
    assert "mode=DRY RUN" in text and "@" not in text
    assert "Test Officer In Charge" in text and "Real Name" not in text
    for u in (oic, op, fake):
        assert f"user id={u.pk} [flag]" in text
    assert f"user id={keep.pk}" not in text
    admin = UserFactory(user_type=UserType.ADMIN, name="Test Admin")
    assert f"user id={admin.pk} [protected]" in _dry()
    assert f"equipment id={laser.equipment_id} [flag]" in text
    assert f"equipment id={iictest.equipment_id} [flag]" in text
    assert f"equipment id={tensile.equipment_id}" not in text
    assert not TestDataFlag.objects.filter(object_id=laser.equipment_id).exists()

    call_command(
        "flag_test_data", "--apply", "--equipment-ids", str(laser.equipment_id), "--skip-users", str(fake.pk),
        stdout=StringIO(),
    )
    for u in (oic, op, fake, admin):
        u.refresh_from_db()
    assert oic.is_test_account and op.is_test_account and not fake.is_test_account and not admin.is_test_account
    assert {laser.equipment_id, iictest.equipment_id} <= testdata.get_test_equipment_ids()
    assert tensile.equipment_id not in testdata.get_test_equipment_ids()
