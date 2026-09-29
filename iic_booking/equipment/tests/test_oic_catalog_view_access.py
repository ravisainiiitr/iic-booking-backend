"""OIC "All equipment" catalog: read-only access to equipment the OIC does not manage."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.models import Equipment, EquipmentManager
from iic_booking.users.models import Department
from iic_booking.users.models.user_group import UserGroup
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _equipment(**kwargs):
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"CV{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _department(*, visible: bool) -> Department:
    tag = uuid.uuid4().hex[:5]
    return Department.objects.create(name=f"Dept {tag}", code=f"CV{tag}", equipment_visibility_enabled=visible)


@pytest.fixture
def oic_setup():
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    dept = _department(visible=True)
    mine = _equipment(internal_department=dept)
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    other = _equipment(internal_department=dept)
    return oic, mine, other


def test_oic_can_view_unmanaged_catalog_equipment_read_only(oic_setup):
    oic, mine, other = oic_setup

    res = _client(oic).get(f"/api/equipments/{other.pk}/")
    assert res.status_code == 200, res.data
    assert res.data["viewer_catalog_only"] is True

    own = _client(oic).get(f"/api/equipments/{mine.pk}/")
    assert own.status_code == 200
    assert own.data["viewer_catalog_only"] is False

    assert _client(oic).get(f"/api/equipments/{other.pk}/ratings/").status_code == 200

    patched = _client(oic).patch(f"/api/equipments/{other.pk}/", {"name": "Renamed"}, format="json")
    assert patched.status_code == 403
    other.refresh_from_db()
    assert other.name != "Renamed"


def test_oic_slots_for_unmanaged_equipment_use_end_user_view(oic_setup):
    oic, mine, other = oic_setup
    real_serializer = api_views.DailySlotSerializer

    with patch.object(api_views, "DailySlotSerializer", wraps=real_serializer) as spy:
        res = _client(oic).get(f"/api/equipments/{other.pk}/slots/")
        assert res.status_code == 200, res.data
        assert spy.call_args.kwargs["context"]["include_booking_user_contact"] is False

        own = _client(oic).get(f"/api/equipments/{mine.pk}/slots/")
        assert own.status_code == 200, own.data
        assert spy.call_args.kwargs["context"]["include_booking_user_contact"] is True


def test_oic_cannot_view_group_restricted_equipment_they_do_not_manage(oic_setup):
    oic, mine, _ = oic_setup
    suffix = uuid.uuid4().hex[:6]
    group = UserGroup.objects.create(name=f"Private {suffix}", code=f"PRV{suffix}")
    private = _equipment(visibility_group=group, internal_department=mine.internal_department)
    assert _client(oic).get(f"/api/equipments/{private.pk}/").status_code == 403
    assert _client(oic).get(f"/api/equipments/{private.pk}/slots/").status_code == 403


def test_oic_all_catalog_respects_department_visibility(oic_setup):
    oic, mine, other = oic_setup
    hidden_dept = _department(visible=False)
    hidden = _equipment(internal_department=hidden_dept)
    own_hidden = _equipment(internal_department=hidden_dept)
    EquipmentManager.objects.create(equipment=own_hidden, manager=oic)

    listed = {
        e["equipment_id"]
        for e in _client(oic).get("/api/equipments/", {"catalog_scope": "all"}).data["equipments"]
    }
    assert {mine.pk, other.pk, own_hidden.pk} <= listed
    assert hidden.pk not in listed

    assert _client(oic).get(f"/api/equipments/{hidden.pk}/").status_code == 403
    own = _client(oic).get(f"/api/equipments/{own_hidden.pk}/")
    assert own.status_code == 200
    assert own.data["viewer_catalog_only"] is False


def test_lab_incharge_still_limited_to_assigned_equipment():
    operator = UserFactory(admin_approved=True, user_type=UserType.OPERATOR)
    other = _equipment()
    assert _client(operator).get(f"/api/equipments/{other.pk}/").status_code == 403
