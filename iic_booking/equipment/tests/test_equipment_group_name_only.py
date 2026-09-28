"""Equipment groups are identified by name only: no code in the API, unique names, merge of same-name groups."""

from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import call_command

from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import Equipment, EquipmentGroup, EquipmentGroupQuota
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

GROUPS_URL = "/api/admin/equipment-groups/"


def _main_admin():
    return UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)


def _other_department():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"EGN-Other-{tag}", code=f"EN{tag[:4]}", equipment_booking_enabled=True, equipment_visibility_enabled=True
    )


def _quota(group, quota_type, minutes):
    return EquipmentGroupQuota.objects.create(
        equipment_group=group, quota_type=quota_type, internal_individual_quota_minutes=minutes
    )


@pytest.mark.django_db
def test_create_without_code_and_code_not_in_response(egs_factory):
    client = egs_factory.client_for(_main_admin())

    res = client.post(GROUPS_URL, {"name": "  FE   SEM  "}, format="json")

    assert res.status_code == 201, res.data
    assert "code" not in res.data
    group = EquipmentGroup.objects.get(pk=res.data["equipment_group_id"])
    assert group.name == "FE SEM"
    assert group.code is None
    detail = client.get(f"{GROUPS_URL}{group.pk}/")
    assert detail.status_code == 200
    assert "code" not in detail.data
    listed = client.get(GROUPS_URL)
    rows = listed.data.get("results", listed.data) if isinstance(listed.data, dict) else listed.data
    assert all("code" not in row for row in rows)


@pytest.mark.django_db
def test_create_with_existing_name_is_rejected(egs_factory):
    EquipmentGroup.objects.create(name="FE-SEM")
    client = egs_factory.client_for(_main_admin())

    res = client.post(GROUPS_URL, {"name": " fe-sem ", "code": "OTHER"}, format="json")

    assert res.status_code == 400
    assert egs.GROUP_NAME_TAKEN_MESSAGE in str(res.data)
    assert EquipmentGroup.objects.filter(name__iexact="fe-sem").count() == 1


@pytest.mark.django_db
def test_rename_to_taken_name_is_rejected_and_own_name_is_kept(egs_factory):
    EquipmentGroup.objects.create(name="TEM")
    group = EquipmentGroup.objects.create(name="SEM")
    member = egs_factory.equipment(group)
    client = egs_factory.client_for(_main_admin())
    url = f"{GROUPS_URL}{group.pk}/"

    taken = client.put(url, {"name": "tem", "equipment_ids": []}, format="json")
    assert taken.status_code == 400
    assert taken.data["code"] == "GROUP_NAME_TAKEN"
    group.refresh_from_db()
    member.refresh_from_db()
    assert group.name == "SEM"
    assert member.equipment_group_id == group.pk

    blank = client.put(url, {"name": "   "}, format="json")
    assert blank.status_code == 400
    assert blank.data["code"] == "GROUP_NAME_REQUIRED"

    same = client.put(url, {"name": "SEM", "code": "IGNORED", "equipment_ids": [member.pk]}, format="json")
    assert same.status_code == 200, same.data
    assert "code" not in same.data
    group.refresh_from_db()
    assert group.code is None


@pytest.mark.django_db
def test_django_admin_form_rejects_taken_name():
    from iic_booking.equipment.admin import EquipmentGroupAdminForm

    EquipmentGroup.objects.create(name="Raman")
    form = EquipmentGroupAdminForm(data={"name": "RAMAN", "description": ""})
    assert not form.is_valid()
    assert form.errors["name"] == [egs.GROUP_NAME_TAKEN_MESSAGE]
    ok = EquipmentGroupAdminForm(data={"name": "Raman 2", "description": ""})
    assert ok.is_valid(), ok.errors


@pytest.mark.django_db
def test_equipment_serializers_do_not_expose_group_code(egs_factory):
    from iic_booking.equipment.serializers import EquipmentDetailSerializer, EquipmentListSerializer

    eq = egs_factory.equipment(EquipmentGroup.objects.create(name="XRD"))
    assert "equipment_group_code" not in EquipmentListSerializer.Meta.fields
    assert "equipment_group_code" not in EquipmentDetailSerializer.Meta.fields
    assert eq.equipment_group.name == "XRD"


@pytest.mark.django_db
def test_merge_same_name_groups(egs_factory):
    first = EquipmentGroup.objects.create(name="FE-SEM", code="Carl")
    busiest = EquipmentGroup.objects.create(name="fe-sem ", code="APREO", cross_rescheduling_enabled=True)
    empty = EquipmentGroup.objects.create(name="FE-SEM", alternative_booking_enabled=True)
    other = EquipmentGroup.objects.create(name="TEM")
    a = egs_factory.equipment(first)
    b1 = egs_factory.equipment(busiest)
    b2 = egs_factory.equipment(busiest)
    t = egs_factory.equipment(other)
    _quota(busiest, "WEEKLY", 100)
    _quota(first, "WEEKLY", 999)
    _quota(first, "MONTHLY", 500)

    dry = egs.merge_equipment_groups_by_name()
    assert dry == [
        {
            "name": busiest.name,
            "keep_id": busiest.pk,
            "merge_ids": [first.pk, empty.pk],
            "moved_equipment": [a.code],
            "skipped": None,
        }
    ]
    assert EquipmentGroup.objects.filter(pk__in=[first.pk, empty.pk]).count() == 2
    a.refresh_from_db()
    assert a.equipment_group_id == first.pk

    applied = egs.merge_equipment_groups_by_name(apply=True)
    assert applied == dry
    assert not EquipmentGroup.objects.filter(pk__in=[first.pk, empty.pk]).exists()
    assert set(Equipment.objects.filter(equipment_group=busiest).values_list("pk", flat=True)) == {a.pk, b1.pk, b2.pk}
    t.refresh_from_db()
    assert t.equipment_group_id == other.pk
    quotas = {q.quota_type: q.internal_individual_quota_minutes for q in EquipmentGroupQuota.objects.filter(equipment_group=busiest)}
    assert quotas == {"WEEKLY": 100, "MONTHLY": 500}
    busiest.refresh_from_db()
    assert busiest.cross_rescheduling_enabled is True
    assert busiest.alternative_booking_enabled is True
    assert busiest.auto_allocation_enabled is False

    assert egs.merge_equipment_groups_by_name(apply=True) == []


@pytest.mark.django_db
def test_merge_keeps_lowest_id_on_tie_and_skips_cross_department(egs_factory):
    tie_low = EquipmentGroup.objects.create(name="AFM")
    tie_high = EquipmentGroup.objects.create(name="AFM")
    egs_factory.equipment(tie_low)
    moved = egs_factory.equipment(tie_high)
    split_a = EquipmentGroup.objects.create(name="NMR")
    split_b = EquipmentGroup.objects.create(name="NMR")
    egs_factory.equipment(split_a)
    foreign = egs_factory.equipment(split_b, internal_department=_other_department())

    report = {entry["name"]: entry for entry in egs.merge_equipment_groups_by_name(apply=True)}

    assert report["AFM"]["keep_id"] == tie_low.pk
    moved.refresh_from_db()
    assert moved.equipment_group_id == tie_low.pk
    assert report["NMR"]["skipped"]
    assert EquipmentGroup.objects.filter(pk__in=[split_a.pk, split_b.pk]).count() == 2
    foreign.refresh_from_db()
    assert foreign.equipment_group_id == split_b.pk


@pytest.mark.django_db
def test_merge_command_is_dry_run_by_default(egs_factory):
    keep = EquipmentGroup.objects.create(name="ICP")
    dup = EquipmentGroup.objects.create(name="ICP")
    egs_factory.equipment(keep)

    out = StringIO()
    call_command("merge_equipment_groups_by_name", stdout=out)
    assert "DRY RUN" in out.getvalue()
    assert EquipmentGroup.objects.filter(pk=dup.pk).exists()

    out = StringIO()
    call_command("merge_equipment_groups_by_name", "--apply", stdout=out)
    assert "APPLIED" in out.getvalue()
    assert not EquipmentGroup.objects.filter(pk=dup.pk).exists()
