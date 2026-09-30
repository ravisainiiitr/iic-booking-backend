"""Per-equipment OIC / Lab Operator contact fields, and ICPMS element symbols shown in chemical case."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import EquipmentManager, EquipmentOperator, ICPMSStandardSample
from iic_booking.equipment.serializers import (
    EquipmentManagerSerializer,
    EquipmentOperatorSerializer,
    _create_related,
    _sync_related,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_create_related_stores_contact_fields(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER)
    operator = UserFactory(user_type=UserType.OPERATOR)

    _create_related(
        eq,
        {
            "equipment_managers": [
                {"manager": oic, "office_address": "  Room 101, IIC  ", "alternate_phone_number": "01332-111111"}
            ],
            "equipment_operators": [
                {"operator": operator, "role": "PRIMARY", "office_address": "Lab 3", "alternate_phone_number": "99"}
            ],
        },
    )

    mgr_row = EquipmentManager.objects.get(equipment=eq)
    assert mgr_row.office_address == "Room 101, IIC"
    assert mgr_row.alternate_phone_number == "01332-111111"
    op_row = EquipmentOperator.objects.get(equipment=eq)
    assert (op_row.office_address, op_row.alternate_phone_number) == ("Lab 3", "99")

    assert EquipmentManagerSerializer(mgr_row).data["office_address"] == "Room 101, IIC"
    op_data = EquipmentOperatorSerializer(op_row, context={}).data
    assert op_data["alternate_phone_number"] == "99"


@pytest.mark.django_db
def test_sync_related_keeps_contacts_when_client_omits_them(egs_factory):
    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(
        equipment=eq, manager=oic, office_address="Room 7", alternate_phone_number="12345"
    )

    _sync_related(eq, {"equipment_managers": [{"manager": oic, "disable_booking_confirmation_email": True}]})

    row = EquipmentManager.objects.get(equipment=eq)
    assert row.disable_booking_confirmation_email is True
    assert (row.office_address, row.alternate_phone_number) == ("Room 7", "12345")

    _sync_related(eq, {"equipment_managers": [{"manager": oic, "office_address": "", "alternate_phone_number": ""}]})

    row = EquipmentManager.objects.get(equipment=eq)
    assert (row.office_address, row.alternate_phone_number) == ("", "")


@pytest.mark.django_db
def test_icpms_endpoints_return_element_symbols_in_chemical_case():
    ICPMSStandardSample.objects.create(s_no="STD - 1", name_of_std="Mix", list_of_elements="AG, al, Cu, U", status=1)
    client = APIClient()

    cover = client.post("/api/icpms/standards/min-cover/", {"elements": ["ag", "CU", "u"]}, format="json")
    assert cover.status_code == 200, cover.data
    assert cover.data["elements"] == ["Ag", "Cu", "U"]
    assert cover.data["standards"][0]["list_of_elements"] == "Ag, Cu, U"

    missing = client.post("/api/icpms/standards/min-cover/", {"elements": ["AG", "HG"]}, format="json")
    assert missing.data["uncovered"] == ["Hg"]

    available = client.post("/api/icpms/standards/available/", {"elements": ["AL"]}, format="json")
    assert available.status_code == 200, available.data
    assert available.data["standards"][0]["list_of_elements"] == "Al"
