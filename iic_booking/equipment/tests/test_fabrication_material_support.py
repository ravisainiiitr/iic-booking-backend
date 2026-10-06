"""Per-equipment material support: the Fabrication Materials list is the master list and each 3D printer /
laser cutter supports materials of its own category from it. Users see supported AND enabled materials."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import transaction

from iic_booking.equipment.calculators import ChargeCalculationEngine
from iic_booking.equipment.duplicate import duplicate_equipment
from iic_booking.equipment.fabrication import inject_print_parts, merge_laser_booking_into_input_values
from iic_booking.equipment.fabrication_material_support import (
    bookable_materials,
    set_supported_materials,
    supported_materials,
)
from iic_booking.equipment.laser_cut_views import apply_laser_part_changes
from iic_booking.equipment.models import (
    ChargeProfile,
    EquipmentManager,
    EquipmentProfileType,
    LaserCutBatch,
    PrintMaterial,
)
from iic_booking.equipment.print_3d_views import merge_print_booking_into_input_values
from iic_booking.equipment.serializers import EquipmentDetailSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import (
    acrylic_3mm,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
    print_part,
)

pytestmark = pytest.mark.django_db

MANAGE_URL = "/api/oic/fabrication-materials/equipment/"


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _ids(qs):
    return sorted(qs.values_list("pk", flat=True))


# --------------------------------------------------------------------------- category restriction


def test_new_material_is_supported_by_the_equipment_it_was_added_for(egs_factory):
    printer = print_equipment(egs_factory)
    laser = laser_equipment(egs_factory)
    pla = print_material(printer)
    acr = acrylic_3mm(laser)
    assert _ids(supported_materials(printer)) == [pla.pk]
    assert _ids(supported_materials(laser)) == [acr.pk]


def test_print_equipment_cannot_support_sheet_materials_and_vice_versa(egs_factory):
    printer = print_equipment(egs_factory)
    laser = laser_equipment(egs_factory)
    pla = print_material(printer)
    acr = acrylic_3mm(laser)

    # The API takes ids from the equipment's own category list; anything else is refused.
    missing = max(PrintMaterial.objects.order_by("-pk").values_list("pk", flat=True)[:1]) + 1000
    _m, err = set_supported_materials(printer, [pla.pk, missing])
    assert err and "not 3D print materials" in err
    plain = egs_factory.equipment()
    _m, err = set_supported_materials(plain, [pla.pk])
    assert err and "Only 3D print or laser cutting equipment" in err
    assert not plain.supported_print_materials.exists()

    # Enforced on the link table itself, whichever side writes it.
    for write in (
        lambda: laser.supported_print_materials.add(pla),
        lambda: acr.supported_equipment.add(printer),
        lambda: pla.supported_equipment.add(plain),
        lambda: printer.supported_laser_sheet_materials.add(acr),
    ):
        with pytest.raises(ValidationError), transaction.atomic():
            write()
    assert _ids(supported_materials(printer)) == [pla.pk]
    assert _ids(supported_materials(laser)) == [acr.pk]


def test_one_equipment_cannot_support_two_materials_with_the_same_code(egs_factory):
    a = print_equipment(egs_factory)
    b = print_equipment(egs_factory)
    pla_a = print_material(a, code="PLA")
    pla_b = print_material(b, code="pla")
    _m, err = set_supported_materials(a, [pla_a.pk, pla_b.pk])
    assert err and "share the code" in err
    with pytest.raises(ValidationError), transaction.atomic():
        a.supported_print_materials.add(pla_b)
    assert _ids(supported_materials(a)) == [pla_a.pk]


def test_equipment_supports_materials_added_for_other_equipment(egs_factory):
    a = print_equipment(egs_factory)
    b = print_equipment(egs_factory)
    pla = print_material(a, code="PLA")
    abs_ = print_material(b, code="ABS", name="ABS")
    _m, err = set_supported_materials(a, [pla.pk, abs_.pk])
    assert err is None
    assert _ids(bookable_materials(a)) == sorted([pla.pk, abs_.pk])
    assert _ids(bookable_materials(b)) == [abs_.pk]


def test_profile_type_change_drops_links_but_keeps_the_materials(egs_factory):
    printer = print_equipment(egs_factory)
    pla = print_material(printer)
    printer.refresh_from_db()
    printer.profile_type = EquipmentProfileType.LASER_CUT_2D
    printer.save()
    assert not PrintMaterial.objects.filter(supported_equipment=printer).exists()
    assert PrintMaterial.objects.filter(pk=pla.pk).exists()


# --------------------------------------------------------------------------- management API


def _oic_for(egs_factory, *equipments):
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    for eq in equipments:
        EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def test_manage_api_lists_master_list_and_saves_supported_set(egs_factory):
    mine = print_equipment(egs_factory)
    other = print_equipment(egs_factory)
    laser = laser_equipment(egs_factory)
    pla = print_material(mine, code="PLA")
    abs_ = print_material(other, code="ABS", name="ABS")
    resin = print_material(other, code="RESIN", name="Resin")
    PrintMaterial.objects.filter(pk=resin.pk).update(is_active=False)
    acr = acrylic_3mm(laser)
    client = egs_factory.client_for(_oic_for(egs_factory, mine))

    resp = client.get(MANAGE_URL)
    assert resp.status_code == 200
    master = {m["id"]: m for m in resp.data["master_print_materials"]}
    assert set(master) >= {pla.pk, abs_.pk, resin.pk}
    assert master[pla.pk]["can_edit"] is True and master[abs_.pk]["can_edit"] is False
    assert master[resin.pk]["is_active"] is False
    assert master[abs_.pk]["home_equipment_id"] == other.pk
    assert resp.data["master_laser_sheet_materials"] == []
    row = next(r for r in resp.data["equipments"] if r["equipment_id"] == mine.pk)
    assert row["supported_material_ids"] == [pla.pk]

    resp = client.patch(MANAGE_URL, {"equipment_id": mine.pk, "supported_material_ids": [pla.pk, abs_.pk, resin.pk]},
                        format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["equipment"]["supported_material_ids"] == sorted([pla.pk, abs_.pk, resin.pk])
    # The disabled material is supported but hidden from users until it is re-enabled.
    assert _ids(bookable_materials(mine)) == sorted([pla.pk, abs_.pk])

    missing = max(pla.pk, abs_.pk, resin.pk, acr.pk) + 1000
    resp = client.patch(MANAGE_URL, {"equipment_id": mine.pk, "supported_material_ids": [pla.pk, missing],
                                     "own_material_fixed_charge": "99"}, format="json")
    assert resp.status_code == 400
    assert "not 3D print materials" in resp.data["error"]
    mine.refresh_from_db()
    assert mine.own_material_fixed_charge is None
    assert _ids(supported_materials(mine)) == sorted([pla.pk, abs_.pk, resin.pk])

    resp = client.patch(MANAGE_URL, {"equipment_id": other.pk, "supported_material_ids": []}, format="json")
    assert resp.status_code == 403


def test_code_edits_and_new_materials_cannot_clash_with_supported_codes(egs_factory):
    mine = print_equipment(egs_factory)
    other = print_equipment(egs_factory)
    pla = print_material(mine, code="PLA")
    abs_ = print_material(other, code="ABS", name="ABS")
    mine.supported_print_materials.add(abs_)
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department, admin_approved=True)
    client = egs_factory.client_for(admin)

    resp = client.patch(f"/api/oic/print-materials/{pla.pk}/", {"code": "abs"}, format="json")
    assert resp.status_code == 400 and "already supports" in resp.data["error"]
    resp = client.post("/api/oic/print-materials/",
                       {"equipment_id": mine.pk, "code": "ABS", "name": "ABS 2", "price_per_gram": "2"}, format="json")
    assert resp.status_code == 400 and "already supports" in resp.data["error"]


# --------------------------------------------------------------------------- what users see


def test_users_see_only_supported_and_enabled_materials(egs_factory):
    printer = print_equipment(egs_factory)
    other = print_equipment(egs_factory)
    pla = print_material(printer, code="PLA")
    petg = print_material(printer, code="PETG", name="PETG")
    foreign = print_material(other, code="ABS", name="ABS")
    printer.supported_print_materials.add(foreign)
    printer.supported_print_materials.remove(petg)
    student = egs_factory.student()
    client = egs_factory.client_for(student)

    def listed():
        resp = client.get(f"/api/equipments/{printer.pk}/print-materials/")
        assert resp.status_code == 200
        return sorted(m["id"] for m in resp.data["materials"])

    assert listed() == sorted([pla.pk, foreign.pk])
    PrintMaterial.objects.filter(pk=foreign.pk).update(is_active=False)
    assert listed() == [pla.pk]
    # Disabling keeps the link, so re-enabling brings it back everywhere it is supported.
    assert printer.supported_print_materials.filter(pk=foreign.pk).exists()
    PrintMaterial.objects.filter(pk=foreign.pk).update(is_active=True)
    assert listed() == sorted([pla.pk, foreign.pk])

    detail = EquipmentDetailSerializer(printer).data
    assert sorted(m["id"] for m in detail["bookable_print_materials"]) == sorted([pla.pk, foreign.pk])
    assert len(detail["print_materials"]) == 2  # the equipment form still edits its own materials

    printer.supported_print_materials.clear()
    resp = client.get(f"/api/equipments/{printer.pk}/print-materials/")
    assert resp.data["materials"] == []
    assert resp.data["no_materials_message"] == "No materials configured — contact the OIC."


def test_laser_users_see_only_supported_and_enabled_sheets(egs_factory):
    laser = laser_equipment(egs_factory)
    acr = acrylic_3mm(laser)
    mdf = acrylic_3mm(laser, code="MDF-3", name="MDF 3 mm", rate="2505.60")
    laser.supported_laser_sheet_materials.remove(mdf)
    client = egs_factory.client_for(egs_factory.student())
    resp = client.get(f"/api/equipments/{laser.pk}/laser-sheet-materials/")
    assert [m["id"] for m in resp.data["materials"]] == [acr.pk]
    acr.is_active = False
    acr.save()
    resp = client.get(f"/api/equipments/{laser.pk}/laser-sheet-materials/")
    assert resp.data["materials"] == []
    assert EquipmentDetailSerializer(laser).data["bookable_laser_sheet_materials"] == []


# --------------------------------------------------------------------------- booking validation


def test_print_booking_requires_a_supported_enabled_material(egs_factory, media_tmp):
    printer = print_equipment(egs_factory)
    other = print_equipment(egs_factory)
    student = egs_factory.student()
    pla = print_material(printer, code="PLA")
    foreign = print_material(other, code="ABS", name="ABS")

    ok_part = print_part(printer, student, pla)
    _vals, err = merge_print_booking_into_input_values(printer, {}, student, print_analysis_id=ok_part.pk)
    assert err is None

    bad_part = print_part(printer, student, foreign, name="bad")
    _vals, err = merge_print_booking_into_input_values(printer, {}, student, print_analysis_id=bad_part.pk)
    assert err and "no longer available on this equipment" in err

    printer.supported_print_materials.add(foreign)
    _vals, err = merge_print_booking_into_input_values(printer, {}, student, print_analysis_id=bad_part.pk)
    assert err is None

    PrintMaterial.objects.filter(pk=pla.pk).update(is_active=False)
    _vals, err = merge_print_booking_into_input_values(printer, {}, student, print_analysis_id=ok_part.pk)
    assert err and "no longer available" in err


def test_existing_print_booking_is_still_priced_after_its_material_is_unsupported(egs_factory, media_tmp):
    printer = print_equipment(egs_factory, hourly_rate="60.00")
    other = print_equipment(egs_factory)
    student = egs_factory.student()
    foreign = print_material(other, code="ABS", name="ABS", price="2.0000")
    printer.supported_print_materials.add(foreign)
    part = print_part(printer, student, foreign, weight="10", minutes=60)
    inputs = inject_print_parts({}, [part])
    cp = ChargeProfile.objects.get(equipment=printer)
    before, _ = ChargeCalculationEngine.calculate_charge(cp, inputs, 60)

    printer.supported_print_materials.remove(foreign)
    after, _ = ChargeCalculationEngine.calculate_charge(cp, inputs, 60)
    assert before == after == Decimal("80")  # 10 g x 2.00 + 60 min x 60/h


def test_laser_booking_requires_supported_enabled_sheets(egs_factory, media_tmp):
    laser = laser_equipment(egs_factory)
    student = egs_factory.student()
    acr = acrylic_3mm(laser)
    batch = LaserCutBatch.objects.create(equipment=laser, user=student)
    part = laser_part(laser, student, acr, batch=batch)

    _vals, err, _b = merge_laser_booking_into_input_values(laser, {}, student, laser_cut_batch_id=batch.pk)
    assert err is None

    laser.supported_laser_sheet_materials.remove(acr)
    _vals, err, _b = merge_laser_booking_into_input_values(laser, {}, student, laser_cut_batch_id=batch.pk)
    assert err and "no longer available on this machine" in err

    # Picking an unsupported sheet is refused; re-sending the part's current sheet is not.
    assert apply_laser_part_changes(part, {"material_id": acr.pk}, equipment=laser) is None
    mdf = acrylic_3mm(laser, code="MDF-3", name="MDF 3 mm", rate="2505.60")
    laser.supported_laser_sheet_materials.remove(mdf)
    assert apply_laser_part_changes(part, {"material_id": mdf.pk}, equipment=laser) is not None
    laser.supported_laser_sheet_materials.add(mdf)
    assert apply_laser_part_changes(part, {"material_id": mdf.pk}, equipment=laser) is None


# --------------------------------------------------------------------------- migration and duplication


def test_migration_links_each_material_to_its_own_equipment_only(egs_factory):
    printer = print_equipment(egs_factory)
    laser = laser_equipment(egs_factory)
    pla = print_material(printer, code="PLA")
    off = print_material(printer, code="OFF", name="Old")
    PrintMaterial.objects.filter(pk=off.pk).update(is_active=False)
    acr = acrylic_3mm(laser)
    stray = print_material(laser, code="STRAY")  # owner is not a 3D printer: never offered before
    other = print_equipment(egs_factory)
    print_material(other, code="ABS", name="ABS")
    visible_before = {eq.pk: _ids(PrintMaterial.objects.filter(equipment=eq, is_active=True)) for eq in (printer, other)}

    for model in (PrintMaterial, type(acr)):
        model.supported_equipment.through.objects.all().delete()
    migration = importlib.import_module("iic_booking.equipment.migrations.0233_fabrication_material_support")
    migration.link_materials_to_own_equipment(django_apps, None)
    migration.link_materials_to_own_equipment(django_apps, None)  # idempotent

    assert _ids(supported_materials(printer)) == sorted([pla.pk, off.pk])
    assert _ids(supported_materials(laser)) == [acr.pk]
    assert not stray.supported_equipment.exists()
    for eq in (printer, other):
        assert _ids(bookable_materials(eq)) == visible_before[eq.pk]


def test_duplicate_shares_supported_materials_instead_of_cloning_them(egs_factory):
    printer = print_equipment(egs_factory)
    pla = print_material(printer, code="PLA")
    before = PrintMaterial.objects.count()
    copy, _warnings = duplicate_equipment(printer, copy_image=False)
    assert PrintMaterial.objects.count() == before
    assert _ids(supported_materials(copy)) == [pla.pk]
