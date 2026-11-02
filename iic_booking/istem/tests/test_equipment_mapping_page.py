"""I-STEM equipment mapping page: Main Admin maps every instrument, a department administrator only their own
department's; I-STEM is only read (Equipment List). Fake transport, no network."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import ChargeProfile, DynamicInputField, Equipment
from iic_booking.istem.choices import SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.equipment_mapping import MappingRefused, save_mapping, search_istem_equipment
from iic_booking.istem.models import IstemBookingLink, IstemEquipmentMapping
from iic_booking.users.models import Department
from iic_booking.users.models.rbac import DeptAdminPermissionGrant, PermissionDefinition
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import ensure_default_permission_definitions
from iic_booking.users.tests.factories import UserFactory

from .support import FakeTransport, apply_live, json_response, make_booking, token_response

pytestmark = pytest.mark.django_db

URL = "/api/istem/equipment-mappings/"


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


def _department(tag):
    return Department.objects.create(name=f"Dept {tag}", code=f"D{tag}{uuid.uuid4().hex[:4]}",
                                     equipment_booking_enabled=True, equipment_visibility_enabled=True)


def _equipment(department, name="XRF", *, fbr=False, keys=("A",)):
    equipment = Equipment.objects.create(name=name, code=f"EQ{uuid.uuid4().hex[:6].upper()}", slot_duration_minutes=60,
                                         user_rating_enabled=False, internal_department=department)
    for key in keys:
        DynamicInputField.objects.create(equipment=equipment, field_key=key, field_label=f"Field {key}")
    if fbr:
        ChargeProfile.objects.create(equipment=equipment, user_type="RND", profile_type="HOUR", primary_unit_charge=1000,
                                     require_istem_fbr=True)
    return equipment


def _admin():
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


def _dept_admin(department, *, grant=True):
    user = UserFactory(user_type=UserType.DEPT_ADMIN, department=department, admin_approved=True)
    if grant:
        ensure_default_permission_definitions()
        DeptAdminPermissionGrant.objects.create(department_id=department.pk, dept_admin=user,
                                                permission=PermissionDefinition.objects.get(code="equipment.manage"))
    return user


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _page(rows):
    return json_response({"status": "SUCCESS", "message": "Equipment Found", "totalrecord": len(rows), "records": len(rows),
                          "result": rows})


def test_main_admin_sees_every_instrument_with_its_mapping():
    chem, phys = _department("C"), _department("P")
    xrf = _equipment(chem, "XRF", fbr=True)
    _equipment(phys, "FESEM")
    IstemEquipmentMapping.objects.create(equipment=xrf, istem_equipment_id="38750", sample_count_input_key="A")
    response = _api(_admin()).get(URL)
    assert response.status_code == 200 and response.data["all_departments"] is True
    rows = {r["name"]: r for r in response.data["equipment"]}
    assert set(rows) >= {"XRF", "FESEM"}
    assert rows["XRF"]["requires_istem_fbr"] and rows["XRF"]["mapping"]["istem_equipment_id"] == "38750"
    assert rows["FESEM"]["mapping"] is None and rows["XRF"]["input_fields"] == [{"key": "A", "label": "Field A"}]


def test_a_department_administrator_sees_only_their_department():
    chem, phys = _department("C"), _department("P")
    _equipment(chem, "XRF")
    _equipment(phys, "FESEM")
    response = _api(_dept_admin(chem)).get(URL)
    assert response.status_code == 200 and response.data["all_departments"] is False
    assert [r["name"] for r in response.data["equipment"]] == ["XRF"]


def test_without_equipment_management_the_page_is_refused():
    chem = _department("C")
    assert _api(_dept_admin(chem, grant=False)).get(URL).status_code == 403
    assert _api(UserFactory(user_type=UserType.STUDENT, department=chem, admin_approved=True)).get(URL).status_code == 403


def test_a_department_administrator_cannot_map_another_departments_instrument():
    chem, phys = _department("C"), _department("P")
    fesem = _equipment(phys, "FESEM")
    response = _api(_dept_admin(chem)).put(f"{URL}{fesem.pk}/", {"istem_equipment_id": "12206"}, format="json")
    assert response.status_code == 404
    assert not IstemEquipmentMapping.objects.filter(equipment=fesem).exists()


def test_saving_a_mapping_records_who_set_it():
    chem = _department("C")
    xrf = _equipment(chem, "XRF")
    user = _dept_admin(chem)
    response = _api(user).put(f"{URL}{xrf.pk}/", {"istem_equipment_id": "38750", "sample_count_input_key": "a"}, format="json")
    assert response.status_code == 200, response.data
    mapping = IstemEquipmentMapping.objects.get(equipment=xrf)
    assert (mapping.istem_equipment_id, mapping.sample_count_input_key, mapping.verified_by_id) == ("38750", "A", user.pk)
    assert response.data["checked"] is False  # I-STEM not connected in tests


@pytest.mark.parametrize("body, message", [
    ({"istem_equipment_id": "XRF-1"}, "is a number"),
    ({"istem_equipment_id": "38750", "sample_count_input_key": "Q"}, "does not exist"),
])
def test_invalid_mappings_are_refused(body, message):
    xrf = _equipment(_department("C"), "XRF")
    response = _api(_admin()).put(f"{URL}{xrf.pk}/", body, format="json")
    assert response.status_code == 400 and message in response.data["error"]


def test_the_id_cannot_change_while_bookings_are_open_on_istem():
    booking = make_booking()
    IstemEquipmentMapping.objects.create(equipment=booking.equipment, istem_equipment_id="38750", sample_count_input_key="A")
    link = IstemBookingLink.objects.create(booking=booking, request_id="95571", sync_state=SyncState.CONFIRMED)
    admin = _admin()
    with pytest.raises(MappingRefused) as refused:
        save_mapping(admin, booking.equipment_id, {"istem_equipment_id": "40000"})
    assert refused.value.http_status == 409
    with pytest.raises(MappingRefused):
        save_mapping(admin, booking.equipment_id, {"istem_equipment_id": "38750", "is_active": False})
    save_mapping(admin, booking.equipment_id, {"istem_equipment_id": "38750", "sample_count_input_key": ""})  # same ID: fine
    link.sync_state = SyncState.EXECUTED
    link.save(update_fields=["sync_state"])
    assert save_mapping(admin, booking.equipment_id, {"istem_equipment_id": "40000"})["mapping"]["istem_equipment_id"] == "40000"


def test_search_needs_istem_connected_and_three_letters():
    admin = _admin()
    with pytest.raises(MappingRefused) as short:
        search_istem_equipment(admin, "xr")
    assert short.value.http_status == 400
    with pytest.raises(MappingRefused) as offline:
        search_istem_equipment(admin, "fluorescence")
    assert offline.value.http_status == 503


def test_search_lists_istem_equipment_and_what_it_is_mapped_to(settings):
    apply_live(settings)
    xrf = _equipment(_department("C"), "XRF")
    IstemEquipmentMapping.objects.create(equipment=xrf, istem_equipment_id="38750")
    transport = FakeTransport(token_response(), _page([
        {"equipment_id": 38750, "equipment_name": "X-ray Fluorescence (XRF) -IIC", "institute_id": 213},
        {"equipment_id": 41001, "equipment_name": "X-ray Fluorescence (portable)", "institute_id": 213},
    ]))
    results = search_istem_equipment(_admin(), "  Fluorescence ", client_factory=lambda: IstemClient(transport))
    assert [r["equipment_id"] for r in results] == ["38750", "41001"]
    assert results[0]["mapped_to"] == [{"equipment_id": xrf.pk, "name": "XRF"}] and results[1]["mapped_to"] == []
    assert transport.calls[-1]["params"]["equipment_name"] == "Fluorescence"


def test_saving_while_connected_checks_the_id_on_istem(settings):
    apply_live(settings)
    xrf = _equipment(_department("C"), "XRF")
    transport = FakeTransport(token_response(), _page([{"equipment_id": 38750, "equipment_name": "X-ray Fluorescence (XRF) -IIC"}]))
    result = save_mapping(_admin(), xrf.pk, {"istem_equipment_id": "38750", "sample_count_input_key": "A"},
                          client_factory=lambda: IstemClient(transport))
    assert result["checked"] is True and result["mapping"]["remote_found"] is True
    assert result["mapping"]["remote_equipment_name"] == "X-ray Fluorescence (XRF) -IIC" and result["warnings"] == []
