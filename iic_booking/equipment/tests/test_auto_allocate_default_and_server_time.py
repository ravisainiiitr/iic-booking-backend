"""Per-equipment default for the alternate auto-allocation option, and the public server clock."""

from __future__ import annotations

import time

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer, EquipmentDetailSerializer


@pytest.mark.django_db
def test_auto_allocate_default_is_off_for_new_equipment(egs_factory):
    eq = egs_factory.equipment()
    eq.refresh_from_db()

    assert eq.auto_allocate_alternative_default is False
    assert EquipmentDetailSerializer(eq).data["auto_allocate_alternative_default"] is False


@pytest.mark.django_db
def test_auto_allocate_default_is_writable_from_admin_form(egs_factory):
    eq = egs_factory.equipment()

    serializer = EquipmentAdminWriteSerializer(eq, data={"auto_allocate_alternative_default": True}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    eq.refresh_from_db()
    assert eq.auto_allocate_alternative_default is True
    assert EquipmentDetailSerializer(eq).data["auto_allocate_alternative_default"] is True


@pytest.mark.django_db
def test_duplicate_equipment_keeps_auto_allocate_default(egs_factory):
    from iic_booking.equipment.duplicate import duplicate_equipment

    eq = egs_factory.equipment(auto_allocate_alternative_default=True)

    copy, _warnings = duplicate_equipment(eq, copy_image=False)

    assert copy.auto_allocate_alternative_default is True


@pytest.mark.django_db
def test_server_time_is_public_and_current():
    before = int(time.time() * 1000)
    resp = APIClient().get("/api/server-time/")
    after = int(time.time() * 1000)

    assert resp.status_code == 200
    assert before - 1000 <= resp.data["epoch_ms"] <= after + 1000
    assert resp.data["server_time"]
    assert isinstance(resp.data["utc_offset_minutes"], int)
    assert resp["Cache-Control"] == "no-store"
