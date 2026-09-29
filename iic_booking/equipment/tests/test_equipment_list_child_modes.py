"""Catalog list flags multi-mode parents that actually have child modes, even under a search."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _equipment(name: str, **kwargs):
    defaults = {
        "name": name,
        "code": f"CM{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


@pytest.fixture
def catalog():
    tag = uuid.uuid4().hex[:5]
    dept = Department.objects.create(name=f"Dept {tag}", code=f"CM{tag}", equipment_visibility_enabled=True)
    nmr = _equipment(f"Nuclear Magnetic Resonance {tag}", enable_multi_mode=True, internal_department=dept)
    xps = _equipment(f"X-ray Photoelectron {tag}", enable_multi_mode=True, internal_department=dept)
    ups = _equipment(f"Ultraviolet Photoelectron {tag}", parent_equipment=xps, internal_department=dept)
    return tag, nmr, xps, ups


def _rows(params: dict) -> dict[int, dict]:
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    client = APIClient()
    client.force_authenticate(user=admin)
    res = client.get("/api/equipments/", params)
    assert res.status_code == 200, res.data
    return {row["equipment_id"]: row for row in res.data["equipments"]}


@pytest.mark.parametrize("include_ratings", ["", "1"])
def test_has_child_modes_ignores_search(catalog, include_ratings):
    tag, nmr, xps, ups = catalog

    rows = _rows({"search": f"Nuclear Magnetic Resonance {tag}", "include_ratings": include_ratings})
    assert set(rows) == {nmr.pk}
    assert rows[nmr.pk]["has_child_modes"] is False

    rows = _rows({"search": f"X-ray Photoelectron {tag}", "include_ratings": include_ratings})
    assert set(rows) == {xps.pk}
    assert rows[xps.pk]["has_child_modes"] is True


def test_has_child_modes_without_search(catalog):
    tag, nmr, xps, ups = catalog
    rows = _rows({"internal_department_id": str(nmr.internal_department_id)})
    assert rows[nmr.pk]["has_child_modes"] is False
    assert rows[xps.pk]["has_child_modes"] is True
    assert rows[ups.pk]["has_child_modes"] is False
