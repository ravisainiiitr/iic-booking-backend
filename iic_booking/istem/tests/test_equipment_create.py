"""Main Admin creates an unmapped instrument on I-STEM (Equipment Create, 8 Nov 2026). Fake transport."""

from __future__ import annotations

import pytest
from django.core.cache import cache

from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.equipment_create import (
    build_payload, create_on_istem, get_details, save_details, sync_reference_lists,
)
from iic_booking.istem.equipment_mapping import MappingRefused
from iic_booking.istem.models import (
    IstemDepartmentMapping, IstemEquipmentDetails, IstemEquipmentMapping, IstemReferenceValue,
)

from .support import FakeTransport, apply_live, json_response, token_response
from .test_equipment_mapping_page import _admin, _api, _department, _dept_admin, _equipment, _page

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh(settings):
    apply_live(settings)
    clear_token_cache()
    cache.clear()
    yield
    clear_token_cache()


def _lists():
    return [
        json_response({"status": "SUCCESS", "message": "Found", "records": 1,
                       "result": [{"id": 109, "val": "Department of Science and Technology"}]}),
        json_response({"status": "SUCCESS", "message": "Found", "records": 2,
                       "result": [{"val": "Internal Funding"}, {"val": "External Funding"}]}),
        json_response({"status": "SUCCESS", "message": "Found", "result": [{"id": 3, "val": "Microscopy"}]}),
        json_response({"status": "ERROR", "message": "No facilities found"}),
    ]


def _factory(transport):
    return lambda: IstemClient(transport)


def _ready(equipment, admin):
    IstemDepartmentMapping.objects.create(istem_department_id="164", department=equipment.internal_department)
    sync_reference_lists(admin, client_factory=_factory(FakeTransport(token_response(), *_lists())))
    clear_token_cache()
    return save_details(admin, equipment.pk, {"equipment_make": "Bruker", "equipment_srno": "SN-42", "istem_department_id": "164",
                                              "funding_agency_id": "109", "funding_agency_type": "Internal Funding",
                                              "supplier_name": "Bruker India", "alert_mailid": "oic.xrf@iitr.ac.in"})


def test_reference_lists_are_stored_with_their_ids():
    out = sync_reference_lists(_admin(), client_factory=_factory(FakeTransport(token_response(), *_lists())))
    assert out["counts"] == {"funding_agency": 1, "funding_agency_type": 2, "equipment_category": 1, "facility": 0}
    agency = IstemReferenceValue.objects.get(kind="funding_agency")
    assert (agency.istem_id, agency.name) == ("109", "Department of Science and Technology")
    assert set(IstemReferenceValue.objects.filter(kind="funding_agency_type").values_list("istem_id", flat=True)) == {""}


def test_details_are_prefilled_and_missing_fields_named():
    department = _department("C")
    equipment = _equipment(department, "XRF")
    equipment.make, equipment.asset_serial_number, equipment.model_information = "Bruker", "", "S8 Tiger"
    equipment.save()
    IstemDepartmentMapping.objects.create(istem_department_id="164", department=department)
    body = get_details(_admin(), equipment.pk)
    assert body["saved"] is False
    assert body["details"]["equipment_make"] == "Bruker" and body["details"]["equipment_model"] == "S8 Tiger"
    assert body["details"]["istem_department_id"] == "164"
    assert set(body["problems"]) == {"equipment_srno", "funding_agency_id", "funding_agency_type", "alert_mailid"}
    # The two support answers default to No.
    assert (body["details"]["pre_experiment_support"], body["details"]["post_experiment_support"]) == ("No", "No")
    assert body["choices"]["yes_no"] == [{"id": "Yes", "name": "Yes"}, {"id": "No", "name": "No"}]


def test_only_the_main_admin():
    department = _department("C")
    equipment = _equipment(department, "XRF")
    with pytest.raises(MappingRefused):
        get_details(_dept_admin(department), equipment.pk)
    assert _api(_dept_admin(department)).get(f"/api/istem/equipment-details/{equipment.pk}/").status_code == 403


def test_payload_sends_free_text_supplier_as_other():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    payload = build_payload(IstemEquipmentDetails.objects.get(equipment=equipment), "213")
    assert payload["supplier_name"] == "Other" and payload["suppl"] == "Bruker India"
    assert payload["equipment_department_id"] == "164" and payload["equipment_agency_id"] == "109"
    assert payload["institute_id"] == "213"


def test_create_stores_the_new_id_as_a_verified_mapping():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    transport = FakeTransport(token_response(), _page([]),
                              json_response({"status": "SUCCESS", "message": "Equipment added", "equipment_id": 40001}))
    out = create_on_istem(admin, equipment.pk, client_factory=_factory(transport))
    assert out["istem_equipment_id"] == "40001"
    assert transport.paths()[-1] == "api/equipment"
    sent = transport.calls[-1]["data"]
    assert sent["equipment_srno"] == "SN-42" and sent["institute_id"] == "213"
    mapping = IstemEquipmentMapping.objects.get(equipment=equipment)
    assert mapping.istem_equipment_id == "40001" and mapping.verified_at and mapping.sample_count_input_key == "A"
    assert IstemEquipmentDetails.objects.get(equipment=equipment).created_istem_equipment_id == "40001"
    with pytest.raises(MappingRefused):
        create_on_istem(admin, equipment.pk, client_factory=_factory(FakeTransport()))


def test_same_serial_on_istem_is_refused():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    listed = [{"equipment_id": 38750, "equipment_name": "XRF IIC", "institute_id": 213, "equipment_srno": "sn-42"}]
    with pytest.raises(MappingRefused, match="already lists"):
        create_on_istem(admin, equipment.pk, client_factory=_factory(FakeTransport(token_response(), _page(listed))))
    assert not IstemEquipmentMapping.objects.filter(equipment=equipment).exists()


def test_success_without_an_id_is_found_by_serial():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    listed = [{"equipment_id": 40002, "equipment_name": "XRF", "institute_id": 213, "equipment_srno": "SN-42"}]
    transport = FakeTransport(token_response(), _page([]), json_response({"status": "SUCCESS", "message": "Added"}),
                              _page(listed))
    out = create_on_istem(admin, equipment.pk, client_factory=_factory(transport))
    assert out["istem_equipment_id"] == "40002"


def test_refusal_is_recorded_and_nothing_mapped():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    transport = FakeTransport(token_response(), _page([]), json_response({"status": "ERROR", "message": "Invalid department"}))
    with pytest.raises(MappingRefused, match="Invalid department"):
        create_on_istem(admin, equipment.pk, client_factory=_factory(transport))
    assert "Invalid department" in IstemEquipmentDetails.objects.get(equipment=equipment).last_error
    assert not IstemEquipmentMapping.objects.filter(equipment=equipment).exists()


def test_dry_run_sends_nothing_and_incomplete_details_are_refused():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    save_details(admin, equipment.pk, {"equipment_make": "Bruker"})
    with pytest.raises(MappingRefused, match="Complete the I-STEM details"):
        create_on_istem(admin, equipment.pk, client_factory=_factory(FakeTransport()))
    _ready(equipment, admin)
    transport = FakeTransport(token_response())
    out = create_on_istem(admin, equipment.pk, dry_run=True, client_factory=_factory(transport))
    assert out["dry_run"] is True and out["payload"]["equipment_name"] == "XRF"
    assert transport.paths() == ["auth/token"]


def test_already_mapped_instrument_cannot_be_created():
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    IstemEquipmentMapping.objects.create(equipment=equipment, istem_equipment_id="38750")
    with pytest.raises(MappingRefused, match="already mapped"):
        create_on_istem(admin, equipment.pk, client_factory=_factory(FakeTransport()))


def test_every_documented_field_is_sent_and_istem_example_values_are_accepted():
    """I-STEM's own Swagger example: empty optional fields sent as "", category by name, website as a bare domain."""
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    out = save_details(admin, equipment.pk, {"category": "Microscopy", "equipment_website": "test.com",
                                             "equipment_rate": "10.00", "facility_id": "1"})
    assert out["problems"] == {}
    payload = build_payload(IstemEquipmentDetails.objects.get(equipment=equipment), "213")
    assert payload["category"] == "Microscopy" and payload["equipment_website"] == "test.com"
    assert payload["equipment_project_id"] == "" and payload["pooling_status"] == "" and payload["last_amc_done"] == ""
    assert payload["equipment_fov_rate"] == "" and payload["supplier_email_id"] == ""
    assert save_details(admin, equipment.pk, {"category": "Unknown"})["problems"]["category"]


def test_alert_email_and_support_answers_are_sent_and_checked():
    """[STAGING 2026-11-12] I-STEM refuses Equipment Create without alert_mailid, pre_experiment_support and
    post_experiment_support (none of them in its Swagger)."""
    admin = _admin()
    equipment = _equipment(_department("C"), "XRF")
    _ready(equipment, admin)
    payload = build_payload(IstemEquipmentDetails.objects.get(equipment=equipment), "213")
    assert (payload["alert_mailid"], payload["pre_experiment_support"], payload["post_experiment_support"]) == (
        "oic.xrf@iitr.ac.in", "No", "No")
    problems = save_details(admin, equipment.pk, {"alert_mailid": "not-an-email", "pre_experiment_support": "Maybe"})["problems"]
    assert set(problems) == {"alert_mailid", "pre_experiment_support"}
    assert save_details(admin, equipment.pk, {"alert_mailid": "lab@iitr.ac.in", "pre_experiment_support": "Yes"})["problems"] == {}


def test_alert_email_defaults_to_the_officer_in_charge():
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.tests.factories import UserFactory

    equipment = _equipment(_department("C"), "XRF")
    oic = UserFactory(email="oic.default@iitr.ac.in")
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    assert get_details(_admin(), equipment.pk)["details"]["alert_mailid"] == "oic.default@iitr.ac.in"
