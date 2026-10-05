"""Who may manage fabrication materials, the test-equipment seed command, and material round-trips through the
equipment form serializer (IDs must stay stable because uploads point at them)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command

from iic_booking.equipment.models import (
    Equipment,
    EquipmentManager,
    LaserSheetMaterial,
    PrintMaterial,
)
from iic_booking.equipment.serializers import (
    EquipmentAdminWriteSerializer,
    EquipmentDetailSerializer,
    LaserSheetMaterialWriteSerializer,
    _sync_related,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.rbac import DeptAdminPermissionGrant, PermissionDefinition
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import ensure_default_permission_definitions
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import (
    acrylic_3mm,
    funded_student,
    internal_department,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
)

pytestmark = pytest.mark.django_db


def _user(user_type, department):
    return UserFactory(user_type=user_type, department=department, admin_approved=True)


def _dept_admin(department, *, granted=True):
    user = _user(UserType.DEPT_ADMIN, department)
    if granted:
        ensure_default_permission_definitions()
        DeptAdminPermissionGrant.objects.create(
            department_id=department.pk,
            dept_admin=user,
            permission=PermissionDefinition.objects.get(code="equipment.manage"),
        )
    return user


@pytest.fixture
def setup(egs_factory):
    own_dept = egs_factory.department
    Department.objects.filter(pk=own_dept.pk).update(department_type=DepartmentType.INTERNAL)
    own_dept.refresh_from_db()
    other_dept = internal_department("OT")
    laser = laser_equipment(egs_factory)
    printer = print_equipment(egs_factory)
    other_laser = laser_equipment(egs_factory, internal_department=other_dept)
    oic = _user(UserType.MANAGER, own_dept)
    EquipmentManager.objects.create(equipment=laser, manager=oic)
    return {
        "laser": laser,
        "printer": printer,
        "other_laser": other_laser,
        "admin": _user(UserType.ADMIN, own_dept),
        "oic": oic,
        "dept_admin": _dept_admin(own_dept),
        "dept_admin_no_grant": _dept_admin(own_dept, granted=False),
        "other_dept_admin": _dept_admin(other_dept),
        "student": _user(UserType.STUDENT, own_dept),
        "operator": _user(UserType.OPERATOR, own_dept),
    }


SHEET = {"code": "MDF-3", "name": "MDF 3 mm", "material_family": "MDF", "thickness_mm": "3", "sheet_rate": "2505.60"}


@pytest.mark.parametrize(
    "who, visible, can_add_own_laser, can_add_other_laser",
    [
        ("admin", {"laser", "printer", "other_laser"}, True, True),
        ("oic", {"laser"}, True, False),
        ("dept_admin", {"laser", "printer"}, True, False),
        ("dept_admin_no_grant", set(), False, False),
        ("other_dept_admin", {"other_laser"}, False, True),
        ("student", set(), False, False),
        ("operator", set(), False, False),
    ],
)
def test_material_management_permission_matrix(
    egs_factory, setup, who, visible, can_add_own_laser, can_add_other_laser
):
    client = egs_factory.client_for(setup[who])
    resp = client.get("/api/oic/fabrication-materials/equipment/")
    assert resp.status_code == 200
    ids = {row["equipment_id"] for row in resp.data["equipments"]}
    expected = {setup[name].pk for name in visible}
    assert ids & {setup[n].pk for n in ("laser", "printer", "other_laser")} == expected
    assert resp.data["has_fabrication_equipment"] is bool(ids)

    for key, allowed in (("laser", can_add_own_laser), ("other_laser", can_add_other_laser)):
        resp = client.post(
            "/api/oic/laser-sheet-materials/", {**SHEET, "equipment_id": setup[key].pk}, format="json"
        )
        assert resp.status_code == (201 if allowed else 403), (who, key, resp.data)

    resp = client.patch(
        "/api/oic/fabrication-materials/equipment/",
        {"equipment_id": setup["laser"].pk, "fabrication_notification_emails": "a@iitr.ac.in, b@iitr.ac.in"},
        format="json",
    )
    assert resp.status_code == (200 if can_add_own_laser else 403)
    if can_add_own_laser:
        assert resp.data["equipment"]["fabrication_notification_emails"] == ["a@iitr.ac.in", "b@iitr.ac.in"]
        setup["laser"].refresh_from_db()
        assert setup["laser"].print_3d_stl_notification_email == "a@iitr.ac.in"

    print_allowed = "printer" in visible
    resp = client.post(
        "/api/oic/print-materials/",
        {"equipment_id": setup["printer"].pk, "code": "PLA", "name": "PLA (FDM)", "source_rate": "1440",
         "source_unit": "PER_KG", "density_g_per_cm3": "1.24"},
        format="json",
    )
    assert resp.status_code == (201 if print_allowed else 403), resp.data
    if print_allowed:
        assert Decimal(resp.data["material"]["price_per_gram"]) == Decimal("1.4400")


def test_sheet_material_in_use_cannot_be_deleted_and_emails_are_validated(egs_factory, setup):
    laser = setup["laser"]
    acr = acrylic_3mm(laser)
    student, _sub = funded_student(egs_factory)
    laser_part(laser, student, acr)
    client = egs_factory.client_for(setup["oic"])
    assert client.delete(f"/api/oic/laser-sheet-materials/{acr.pk}/").status_code == 400
    resp = client.patch(f"/api/oic/laser-sheet-materials/{acr.pk}/", {"is_active": False, "sheet_rate": "6100"},
                        format="json")
    assert resp.status_code == 200
    acr.refresh_from_db()
    assert (acr.is_active, acr.sheet_rate) == (False, Decimal("6100.00"))

    resp = client.patch(
        "/api/oic/fabrication-materials/equipment/",
        {"equipment_id": laser.pk, "fabrication_notification_emails": ["not-an-email"]},
        format="json",
    )
    assert resp.status_code == 400
    resp = client.patch(
        "/api/oic/fabrication-materials/equipment/",
        {"equipment_id": laser.pk, "own_material_fixed_charge": "-1"},
        format="json",
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- seed command


def test_seed_command_is_idempotent_and_keeps_admin_edits():
    from iic_booking.equipment.management.commands.seed_fabrication_test_equipment import (
        LASER_CODE,
        PRINTER_CODE,
    )

    Department.objects.get_or_create(
        code="IIC", defaults={"name": "IIC", "department_type": DepartmentType.INTERNAL}
    )
    call_command("seed_fabrication_test_equipment")  # dry run
    assert not Equipment.objects.filter(code__in=[LASER_CODE, PRINTER_CODE]).exists()

    call_command("seed_fabrication_test_equipment", "--apply")
    laser = Equipment.objects.get(code=LASER_CODE)
    printer = Equipment.objects.get(code=PRINTER_CODE)
    assert laser.visible_to_test_accounts_only and printer.visible_to_test_accounts_only
    assert laser.own_material_fixed_charge == Decimal("250.00")
    assert printer.own_material_fixed_charge == Decimal("100.00")
    assert laser.laser_sheet_materials.count() == 10
    assert printer.print_materials.count() == 11
    assert laser.charge_profiles.count() == 6 and laser.slot_masters.count() == 8
    acr3 = laser.laser_sheet_materials.get(code="ACR-3")
    assert (acr3.sheet_rate, acr3.sheet_width_mm, acr3.sheet_height_mm) == (
        Decimal("6018.00"), Decimal("2438.4"), Decimal("1219.2")
    )
    pla = printer.print_materials.get(code="PLA-FDM")
    assert pla.price_per_gram == Decimal("1.4400")
    assert (pla.source_rate, pla.source_unit) == (Decimal("1440.00"), "PER_KG")
    resin = printer.print_materials.get(code="RESIN-GREY-V5")
    assert resin.name == "Resin Grey v5 (Formlabs Form 4L)"
    assert resin.price_per_gram == Decimal("14.2373")  # 16800 / 1000 / 1.18

    # Admin edits survive a re-run; nothing is duplicated.
    LaserSheetMaterial.objects.filter(pk=acr3.pk).update(sheet_rate=Decimal("7000"))
    Equipment.objects.filter(pk=laser.pk).update(own_material_fixed_charge=Decimal("300"))
    ids_before = set(LaserSheetMaterial.objects.filter(equipment=laser).values_list("pk", flat=True))
    call_command("seed_fabrication_test_equipment", "--apply")
    assert set(LaserSheetMaterial.objects.filter(equipment=laser).values_list("pk", flat=True)) == ids_before
    assert Equipment.objects.filter(code__in=[LASER_CODE, PRINTER_CODE]).count() == 2
    acr3.refresh_from_db()
    laser.refresh_from_db()
    assert acr3.sheet_rate == Decimal("7000.00")
    assert laser.own_material_fixed_charge == Decimal("300.00")

    call_command("seed_fabrication_test_equipment", "--apply", "--reset-prices")
    acr3.refresh_from_db()
    laser.refresh_from_db()
    assert acr3.sheet_rate == Decimal("6018.00")
    assert laser.own_material_fixed_charge == Decimal("250.00")


def test_seed_command_requires_the_iic_department():
    from django.core.management.base import CommandError

    Department.objects.filter(code="IIC").delete()
    with pytest.raises(CommandError, match="IIC"):
        call_command("seed_fabrication_test_equipment", "--apply")


# --------------------------------------------------------------------------- serializer round-trip


def _round_trip_laser(equipment, mutate=None):
    data = EquipmentDetailSerializer(equipment, context={"request": None}).data
    rows = [dict(row) for row in data["laser_sheet_materials"]]
    if mutate:
        rows = mutate(rows)
    write = LaserSheetMaterialWriteSerializer(data=rows, many=True)
    assert write.is_valid(), write.errors
    _sync_related(equipment, {"laser_sheet_materials": write.validated_data})


def test_laser_materials_keep_ids_through_the_equipment_form(egs_factory):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    mdf = acrylic_3mm(eq, code="MDF-3", name="MDF 3 mm", rate="2505.60")
    student, _sub = funded_student(egs_factory)
    part = laser_part(eq, student, acr)

    def rename_and_swap(rows):
        for row in rows:
            if row["code"] == "ACR-3":
                row["sheet_rate"] = "6100.00"
                row["code"] = "MDF-3"
            elif row["code"] == "MDF-3":
                row["code"] = "ACR-3"
        return rows

    _round_trip_laser(eq, rename_and_swap)
    acr.refresh_from_db()
    mdf.refresh_from_db()
    assert (acr.code, acr.sheet_rate) == ("MDF-3", Decimal("6100.00"))
    assert mdf.code == "ACR-3"
    part.refresh_from_db()
    assert part.material_id == acr.pk

    # Removing a material that parts use disables it; if another row takes its code, it is renamed.
    def drop_acr_and_take_its_code(rows):
        kept = [row for row in rows if row["id"] == mdf.pk]
        kept[0]["code"] = "MDF-3"
        return kept

    _round_trip_laser(eq, drop_acr_and_take_its_code)
    acr.refresh_from_db()
    mdf.refresh_from_db()
    assert (acr.is_active, acr.code) == (False, f"MDF-3-old-{acr.pk}")
    assert mdf.code == "MDF-3"

    # Unused materials are deleted; used ones stay disabled.
    _round_trip_laser(eq, lambda rows: [])
    assert not LaserSheetMaterial.objects.filter(pk=mdf.pk).exists()
    assert LaserSheetMaterial.objects.filter(pk=acr.pk, is_active=False).exists()

    # A row without an id but with the code of an existing material updates that material (older clients).
    _round_trip_laser(eq, lambda rows: [
        {"code": f"MDF-3-old-{acr.pk}", "name": "Acrylic", "thickness_mm": "3", "sheet_rate": "6200"}
    ])
    acr.refresh_from_db()
    assert (acr.is_active, acr.sheet_rate) == (True, Decimal("6200.00"))
    assert LaserSheetMaterial.objects.filter(equipment=eq).count() == 1


def test_print_materials_keep_ids_and_admin_serializer_leaves_sheets_alone(egs_factory):
    printer = print_equipment(egs_factory)
    pla = print_material(printer)
    data = EquipmentDetailSerializer(printer, context={"request": None}).data
    rows = [dict(row) for row in data["print_materials"]]
    rows[0]["source_rate"] = "1500.00"
    rows[0]["source_unit"] = "PER_KG"
    from iic_booking.equipment.serializers import PrintMaterialWriteSerializer

    write = PrintMaterialWriteSerializer(data=rows, many=True)
    assert write.is_valid(), write.errors
    _sync_related(printer, {"print_materials": write.validated_data})
    assert list(PrintMaterial.objects.filter(equipment=printer).values_list("pk", flat=True)) == [pla.pk]
    pla.refresh_from_db()
    assert pla.price_per_gram == Decimal("1.5000")

    laser = laser_equipment(egs_factory)
    acr = acrylic_3mm(laser)
    serializer = EquipmentAdminWriteSerializer(
        instance=laser,
        data={"own_material_fixed_charge": "250", "fabrication_notification_emails": ["lab@iitr.ac.in"]},
        partial=True,
        context={"request": None},
    )
    assert serializer.is_valid(), serializer.errors
    assert "laser_sheet_materials" not in serializer.validated_data
    serializer.save()
    assert LaserSheetMaterial.objects.filter(pk=acr.pk).exists()
    laser.refresh_from_db()
    assert laser.print_3d_stl_notification_email == "lab@iitr.ac.in"

    dupes = EquipmentAdminWriteSerializer(
        instance=laser,
        data={"laser_sheet_materials": [SHEET, {**SHEET, "code": "mdf-3"}]},
        partial=True,
        context={"request": None},
    )
    assert not dupes.is_valid()
    assert "laser_sheet_materials" in dupes.errors
