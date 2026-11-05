"""OIC-only equipment: only an OIC in the equipment's department can raise a request, on every route, with a full audit."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.config_audit_models import EquipmentConfigAuditLog
from iic_booking.equipment.equipment_group_service import equipment_eligibility_error
from iic_booking.equipment.models import Booking, BookingAttemptLog, BookingEventType, Equipment, EquipmentManager
from iic_booking.equipment.oic_request_audit_models import OicRequestAuditImmutable, OicRequestAuditLog
from iic_booking.equipment.waitlist import add_user_to_waitlist
from iic_booking.equipment.waitlist_booking import create_booking_for_waitlist_user
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWalletTransaction
from iic_booking.users.tests.factories import UserFactory

from .test_booking_template_preferred_slot import _student_with_wallet, no_portal_lock  # noqa: F401

pytestmark = pytest.mark.django_db


def _dept(code="IICX"):
    return Department.objects.get_or_create(code=code, defaults={"name": f"{code} Centre"})[0]


@pytest.fixture
def lab(egs_factory):
    return egs_factory.equipment(oic_requests_only=True, internal_department=_dept())


def _oic_of(f, eq):
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _dept_oic(f, lab):
    """OIC of a different instrument in the lab's department."""
    return _oic_of(f, f.equipment(internal_department=lab.internal_department))


def _outside_oic(f):
    return _oic_of(f, f.equipment(internal_department=_dept("ELSE")))


def _book(f, user, eq, slot, *, oic_registration=True, **extra):
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
        "waitlist_on_failure": False,
        "notes": "Repair of a lab power supply",
        **extra,
    }
    if oic_registration:
        body["oic_registration"] = True
    return f.client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json", HTTP_USER_AGENT="audit-test")


def _rows(eq, **filters):
    return list(OicRequestAuditLog.objects.filter(equipment_id=eq.pk, **filters))


@pytest.mark.parametrize("who", ["own", "department"])
def test_an_oic_in_the_department_raises_a_free_request_in_their_own_name(egs_factory, lab, no_portal_lock, who):  # noqa: F811
    oic = _oic_of(egs_factory, lab) if who == "own" else _dept_oic(egs_factory, lab)
    slot = egs_factory.slot(lab, egs_factory.future(days=3))
    debits = SubWalletTransaction.objects.filter(transaction_type="debit").count()

    res = _book(egs_factory, oic, lab, slot)

    assert res.status_code == 201, res.data
    booking = lab.bookings.get()
    assert booking.user_id == oic.pk and booking.total_charge == Decimal("0.00")
    assert SubWalletTransaction.objects.filter(transaction_type="debit").count() == debits
    (row,) = _rows(lab, action="RAISED")
    assert row.booking_id == booking.pk and row.booking_reference
    assert row.actor_id == oic.pk and row.actor_role == UserType.MANAGER and row.actor_email == oic.email
    assert row.user_agent == "audit-test" and row.ip_address
    assert row.department_id == lab.internal_department_id and row.equipment_name == lab.name


def test_a_department_oic_does_not_gain_management_of_the_lab(egs_factory, lab):
    from iic_booking.equipment.api_views import _user_can_act_as_oic_for_equipment, get_visible_equipment_queryset

    oic = _dept_oic(egs_factory, lab)
    assert not _user_can_act_as_oic_for_equipment(oic, lab)
    assert lab not in get_visible_equipment_queryset(oic)  # managed scope


@pytest.mark.parametrize(
    "make",
    [
        lambda f, lab: _outside_oic(f),
        lambda f, lab: UserFactory(user_type=UserType.ADMIN, department=f.department, admin_approved=True),
        lambda f, lab: UserFactory(user_type=UserType.DEPT_ADMIN, department=lab.internal_department, admin_approved=True),
        lambda f, lab: UserFactory(user_type=UserType.OPERATOR, department=f.department, admin_approved=True),
        lambda f, lab: _student_with_wallet(f)[0],
    ],
    ids=["oic_elsewhere", "main_admin", "dept_admin", "operator", "student"],
)
@pytest.mark.parametrize("oic_registration", [True, False], ids=["as_oic_request", "as_booking"])
def test_everyone_else_is_refused_and_audited(egs_factory, lab, no_portal_lock, make, oic_registration):  # noqa: F811
    user = make(egs_factory, lab)
    slot = egs_factory.slot(lab, egs_factory.future(days=3))

    res = _book(egs_factory, user, lab, slot, oic_registration=oic_registration)

    assert res.status_code == 403 and res.data["code"] == "OIC_REQUESTS_ONLY"
    assert not lab.bookings.exists()
    (row,) = _rows(lab, action="REFUSED")
    assert row.actor_id == user.pk and row.route == "booking" and "Officer In-charge" in row.detail
    assert BookingAttemptLog.objects.filter(equipment=lab, user=user).exists()


def test_an_oic_cannot_book_ordinarily_or_for_someone_else(egs_factory, lab, no_portal_lock):  # noqa: F811
    oic = _oic_of(egs_factory, lab)
    student, _ = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(lab, egs_factory.future(days=3))
    assert _book(egs_factory, oic, lab, slot, oic_registration=False).status_code == 403
    assert _book(egs_factory, oic, lab, slot, oic_registration=False, user_id=student.pk).status_code == 403
    assert not lab.bookings.exists()
    assert len(_rows(lab, action="REFUSED")) == 2

    assert _book(egs_factory, oic, lab, slot, user_id=student.pk).status_code == 201
    assert lab.bookings.get().user_id == oic.pk  # an OIC request is always in the OIC's own name


def test_urgent_requests_are_refused_and_audited(egs_factory, lab, no_portal_lock):  # noqa: F811
    student, _ = _student_with_wallet(egs_factory)
    res = egs_factory.client_for(student).post(
        "/api/urgent-booking-requests/create/", {"equipment_id": lab.pk, "request_type": "NO_SLOT"}, format="json"
    )
    assert res.status_code == 403 and res.data["code"] == "OIC_REQUESTS_ONLY"
    assert _rows(lab, action="REFUSED", route="urgent_request")


def test_repeat_samples_are_refused_and_audited(egs_factory, lab, no_portal_lock):  # noqa: F811
    oic = _oic_of(egs_factory, lab)
    assert _book(egs_factory, oic, lab, egs_factory.slot(lab, egs_factory.future(days=3))).status_code == 201
    booking = lab.bookings.get()
    res = egs_factory.client_for(oic).post(f"/api/bookings/{booking.pk}/create-repeat-booking/", {}, format="json")
    assert res.status_code == 403 and res.data["code"] == "OIC_REQUESTS_ONLY"
    assert _rows(lab, action="REFUSED", route="repeat_sample")


def test_waitlist_and_group_alternatives_are_closed(egs_factory, lab, no_portal_lock):  # noqa: F811
    student, _ = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(lab, egs_factory.future(days=3))
    assert add_user_to_waitlist(lab, student) == (False, None)
    assert create_booking_for_waitlist_user(lab, student, [slot.pk]) == (None, "This equipment takes OIC requests only.")
    assert "OIC requests only" in equipment_eligibility_error(student, lab)


def test_visibility(egs_factory, lab):
    from iic_booking.equipment.api_views import get_visible_equipment_queryset, user_can_see_equipment
    from django.contrib.auth.models import AnonymousUser

    student = egs_factory.student()
    outside = _outside_oic(egs_factory)
    inside = _dept_oic(egs_factory, lab)

    assert not user_can_see_equipment(AnonymousUser(), lab)
    assert not user_can_see_equipment(student, lab)
    assert not user_can_see_equipment(outside, lab)
    assert user_can_see_equipment(inside, lab)
    assert user_can_see_equipment(_oic_of(egs_factory, lab), lab)
    assert user_can_see_equipment(UserFactory(user_type=UserType.ADMIN), lab)

    assert lab not in get_visible_equipment_queryset(AnonymousUser())
    assert lab not in get_visible_equipment_queryset(student)
    assert lab not in get_visible_equipment_queryset(outside, catalog_scope="all")
    assert lab in get_visible_equipment_queryset(inside, catalog_scope="all")
    assert egs_factory.client_for(student).get(f"/api/equipments/{lab.pk}/").status_code in (403, 404)
    from rest_framework.test import APIClient

    assert APIClient().get(f"/api/equipments/{lab.pk}/public/").status_code == 404


def test_other_equipment_is_not_affected(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment(oic_request_registration_enabled=True, internal_department=_dept())
    outsider = _dept_oic(egs_factory, eq)  # same department, not this equipment's OIC
    assert _book(egs_factory, outsider, eq, egs_factory.slot(eq, egs_factory.future(days=3))).status_code == 403
    assert not OicRequestAuditLog.objects.filter(equipment_id=eq.pk).exists()


def test_every_later_event_is_audited(egs_factory, lab, no_portal_lock):  # noqa: F811
    oic = _oic_of(egs_factory, lab)
    assert _book(egs_factory, oic, lab, egs_factory.slot(lab, egs_factory.future(days=3))).status_code == 201
    booking = lab.bookings.get()
    create_booking_event(
        booking, BookingEventType.CANCELLED, created_by=oic, comment="Not needed",
        previous_status="BOOKED", new_status="CANCELLED", send_notification=False,
    )
    (row,) = _rows(lab, action="EVENT", event_type=BookingEventType.CANCELLED)
    assert (row.previous_status, row.new_status, row.detail) == ("BOOKED", "CANCELLED", "Not needed")
    create_booking_event(booking, BookingEventType.STATUS_CHANGED, system_actor=True, send_notification=False)
    (row,) = _rows(lab, event_type=BookingEventType.STATUS_CHANGED)
    assert row.is_system and row.actor_id is None


def test_audit_rows_cannot_be_changed_and_outlive_the_booking(egs_factory, lab, no_portal_lock):  # noqa: F811
    oic = _oic_of(egs_factory, lab)
    assert _book(egs_factory, oic, lab, egs_factory.slot(lab, egs_factory.future(days=3))).status_code == 201
    (row,) = _rows(lab, action="RAISED")
    row.detail = "tampered"
    with pytest.raises(OicRequestAuditImmutable):
        row.save()
    with pytest.raises(OicRequestAuditImmutable):
        row.delete()
    with pytest.raises(OicRequestAuditImmutable):
        OicRequestAuditLog.objects.filter(pk=row.pk).update(detail="x")
    with pytest.raises(OicRequestAuditImmutable):
        OicRequestAuditLog.objects.filter(pk=row.pk).delete()
    Booking.objects.filter(equipment=lab).delete()
    assert _rows(lab, action="RAISED")


def test_audit_api_is_scoped_and_exports_safely(egs_factory, lab, no_portal_lock):  # noqa: F811
    inside = _dept_oic(egs_factory, lab)
    student, _ = _student_with_wallet(egs_factory)
    assert _book(egs_factory, inside, lab, egs_factory.slot(lab, egs_factory.future(days=3))).status_code == 201
    OicRequestAuditLog.objects.create(equipment_id=lab.pk, department_id=lab.internal_department_id, action="REFUSED", detail="=HYPERLINK(1)")

    url = "/api/oic-request-audit/"
    assert egs_factory.client_for(student).get(url).status_code == 403
    assert egs_factory.client_for(_outside_oic(egs_factory)).get(url).json()["count"] == 0
    assert egs_factory.client_for(inside).get(url).json()["count"] == 2
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    data = egs_factory.client_for(admin).get(url, {"equipment_id": lab.pk, "action": "RAISED"}).json()
    assert data["count"] == 1 and data["results"][0]["actor_id"] == inside.pk

    csv_res = egs_factory.client_for(admin).get(url, {"export": "csv"})
    assert csv_res.status_code == 200 and csv_res["Content-Type"].startswith("text/csv")
    body = csv_res.content.decode("utf-8")
    assert "'=HYPERLINK(1)" in body and ",=HYPERLINK" not in body


def test_turning_the_setting_on_is_in_the_configuration_audit(egs_factory):
    eq = egs_factory.equipment()
    eq.oic_requests_only = True
    eq.save()
    (row,) = EquipmentConfigAuditLog.objects.filter(equipment_id=eq.pk, setting="oic_requests_only", action="UPDATED")
    assert (row.previous_value, row.new_value) == ("No", "Yes")


def test_migration_restricts_the_iic_repair_lab_only_and_records_it(egs_factory):
    mig = importlib.import_module("iic_booking.equipment.migrations.0262_oic_requests_only_and_audit")
    iic, _ = Department.objects.get_or_create(code="IIC", defaults={"name": "Institute Instrumentation Centre"})
    lab = egs_factory.equipment(code="ERL IIC", internal_department=iic)
    same_code_elsewhere = egs_factory.equipment(code="ERL IIC2", internal_department=_dept())
    other = egs_factory.equipment(internal_department=iic)

    mig.restrict_repair_lab(django_apps, None)

    for eq in (lab, same_code_elsewhere, other):
        eq.refresh_from_db()
    assert lab.oic_requests_only and lab.oic_request_registration_enabled
    assert lab.status == Equipment.objects.get(pk=lab.pk).status
    assert not same_code_elsewhere.oic_requests_only and not other.oic_requests_only
    row = EquipmentConfigAuditLog.objects.get(equipment_id=lab.pk, setting="oic_requests_only", process__icontains="0262")
    assert row.new_value == "Yes" and row.is_system
