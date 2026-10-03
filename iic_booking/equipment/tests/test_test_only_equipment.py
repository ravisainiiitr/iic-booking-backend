"""Equipment flagged visible_to_test_accounts_only is hidden from everyone except test accounts and the Main Admin."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.api_views import get_visible_equipment_queryset, user_can_see_equipment
from iic_booking.equipment.models import Equipment, EquipmentManager
from iic_booking.sync.installer.services import build_equipment_tree_for_department
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.test_accounts import exclude_test_only_equipment, user_may_see_test_only_equipment
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


@pytest.fixture
def world():
    tag = uuid.uuid4().hex[:5]
    dept = Department.objects.create(name=f"Dept {tag}", code=f"TO{tag}", equipment_visibility_enabled=True)
    public = Equipment.objects.create(
        name=f"Public {tag}", code=f"PUB{tag}", internal_department=dept, user_rating_enabled=False
    )
    hidden = Equipment.objects.create(
        name=f"DSA Test Equipment (TEST) {tag}",
        code=f"TST{tag}",
        internal_department=dept,
        user_rating_enabled=False,
        visible_to_test_accounts_only=True,
    )
    return dept, public, hidden


def _listed(user) -> set[int]:
    res = _client(user).get("/api/equipments/")
    assert res.status_code == 200, res.data
    return {e["equipment_id"] for e in res.data["equipments"]}


def test_flag_defaults_off(world):
    _, public, _ = world
    assert public.visible_to_test_accounts_only is False


def test_anonymous_and_real_student_do_not_see_or_book_it(world):
    _, public, hidden = world
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT, is_test_account=False)

    assert hidden.pk not in _listed(None)
    listed = _listed(student)
    assert public.pk in listed
    assert hidden.pk not in listed

    assert _client(None).get(f"/api/equipments/{hidden.pk}/").status_code == 404
    assert _client(student).get(f"/api/equipments/{hidden.pk}/").status_code == 403
    assert _client(student).get(f"/api/equipments/{hidden.pk}/slots/").status_code == 403
    assert _client(student).post(f"/api/equipments/{hidden.pk}/book/", {}, format="json").status_code == 403
    assert user_can_see_equipment(student, hidden) is False


def test_test_student_and_faculty_see_it(world):
    _, public, hidden = world
    for user_type in (UserType.STUDENT, UserType.FACULTY):
        tester = UserFactory(admin_approved=True, user_type=user_type, is_test_account=True)
        listed = _listed(tester)
        assert {public.pk, hidden.pk} <= listed
        assert _client(tester).get(f"/api/equipments/{hidden.pk}/").status_code == 200
        assert user_can_see_equipment(tester, hidden) is True


def test_real_dept_admin_and_unassigned_oic_do_not_see_it(world):
    dept, _, hidden = world
    dept_admin = UserFactory(admin_approved=True, user_type=UserType.DEPT_ADMIN, department=dept)
    assert user_can_see_equipment(dept_admin, hidden) is False
    assert not get_visible_equipment_queryset(dept_admin).filter(pk=hidden.pk).exists()

    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    assert not get_visible_equipment_queryset(oic, catalog_scope="all").filter(pk=hidden.pk).exists()


def test_real_oic_assigned_to_it_still_does_not_see_it_but_test_oic_does(world):
    _, _, hidden = world
    real_oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER, is_test_account=False)
    test_oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER, is_test_account=True)
    EquipmentManager.objects.create(equipment=hidden, manager=real_oic)
    EquipmentManager.objects.create(equipment=hidden, manager=test_oic)

    assert not get_visible_equipment_queryset(real_oic).filter(pk=hidden.pk).exists()
    assert get_visible_equipment_queryset(test_oic).filter(pk=hidden.pk).exists()


def test_main_admin_sees_it_with_flag_in_payload(world):
    _, _, hidden = world
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    assert user_may_see_test_only_equipment(admin) is True
    res = _client(admin).get("/api/equipments/")
    row = next(e for e in res.data["equipments"] if e["equipment_id"] == hidden.pk)
    assert row["visible_to_test_accounts_only"] is True


def test_exclude_helper_with_prefix(world):
    _, public, hidden = world
    qs = Equipment.objects.filter(pk__in=[public.pk, hidden.pk])
    assert set(exclude_test_only_equipment(qs).values_list("pk", flat=True)) == {public.pk}
    tester = UserFactory(admin_approved=True, user_type=UserType.STUDENT, is_test_account=True)
    assert set(exclude_test_only_equipment(qs, tester).values_list("pk", flat=True)) == {public.pk, hidden.pk}


def test_public_site_stats_do_not_count_it(world):
    before = _client(None).get("/api/cms/site-stats/").data["equipment_count"]
    _, _, hidden = world
    hidden.visible_to_test_accounts_only = False
    hidden.save(update_fields=["visible_to_test_accounts_only"])
    after = _client(None).get("/api/cms/site-stats/").data["equipment_count"]
    assert after == before + 1


def test_dsa_installer_equipment_tree_skips_it(world):
    dept, public, hidden = world
    tree = build_equipment_tree_for_department(dept.pk)
    ids = {eq["id"] for d in tree["departments"] for eq in d["equipment"]}
    assert public.pk in ids
    assert hidden.pk not in ids
