"""Builders shared by the fabrication (3D print / 2D laser cutting) tests."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

from django.core.files.base import ContentFile


def dxf_bytes(*, units: int | None = 4, rects=((0, 0, 200, 100),), circles=(), blocks=None, inserts=()) -> bytes:
    """A DXF with closed rectangles (x, y, w, h), circles (cx, cy, r), optional blocks and inserts."""
    import ezdxf

    doc = ezdxf.new("R2010")
    if units is None:
        doc.header["$INSUNITS"] = 0
    else:
        doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    for x, y, w, h in rects:
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True)
    for cx, cy, r in circles:
        msp.add_circle((cx, cy), r)
    for name, block_rects in (blocks or {}).items():
        block = doc.blocks.new(name=name)
        for x, y, w, h in block_rects:
            block.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True)
    for name, insert_point, scale in inserts:
        msp.add_blockref(name, insert_point, dxfattribs={"xscale": scale, "yscale": scale})
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def internal_department(code_prefix="FB"):
    from iic_booking.users.models import Department
    from iic_booking.users.models.department import DepartmentType

    tag = uuid.uuid4().hex[:5].upper()
    return Department.objects.create(
        name=f"Fab Dept {tag}",
        code=f"{code_prefix}{tag}",
        department_type=DepartmentType.INTERNAL,
        equipment_booking_enabled=True,
        equipment_visibility_enabled=True,
    )


def fabrication_equipment(egs_factory, profile_type, *, hourly_rate="0.00", own_charge=None, emails=None, **kwargs):
    from iic_booking.equipment.models import ChargeProfile, EquipmentProfileType
    from iic_booking.users.models.user_type import UserType

    eq = egs_factory.equipment(
        with_profile=False,
        profile_type=profile_type,
        own_material_fixed_charge=Decimal(own_charge) if own_charge is not None else None,
        fabrication_notification_emails=list(emails or []),
        enable_charge_recalculation=True,
        **kwargs,
    )
    ChargeProfile.objects.create(
        equipment=eq,
        user_type=UserType.STUDENT,
        profile_type=profile_type,
        time_formula="" if profile_type != EquipmentProfileType.HOUR else "60",
        primary_unit_charge=Decimal(hourly_rate),
    )
    return eq


def laser_equipment(egs_factory, **kwargs):
    from iic_booking.equipment.models import EquipmentProfileType

    return fabrication_equipment(egs_factory, EquipmentProfileType.LASER_CUT_2D, **kwargs)


def print_equipment(egs_factory, **kwargs):
    from iic_booking.equipment.models import EquipmentProfileType

    return fabrication_equipment(egs_factory, EquipmentProfileType.PRINT_3D, **kwargs)


def acrylic_3mm(equipment, *, code="ACR-3", rate="6018.00", **kwargs):
    from iic_booking.equipment.models import LaserMaterialFamily, LaserSheetMaterial

    values = {
        "name": "Acrylic sheet 3 mm",
        "material_family": LaserMaterialFamily.ACRYLIC,
        "thickness_mm": Decimal("3"),
        "sheet_width_mm": Decimal("2438.4"),
        "sheet_height_mm": Decimal("1219.2"),
        "sheet_rate": Decimal(rate),
    }
    values.update(kwargs)
    return LaserSheetMaterial.objects.create(equipment=equipment, code=code, **values)


def laser_part(equipment, user, material, *, width="200", height="100", quantity=1, name="bracket", batch=None,
               booking=None, sequence=0, data: bytes | None = None):
    from iic_booking.equipment.models import LaserCutAnalysis, PrintAnalysisStatus

    w, h = Decimal(width), Decimal(height)
    part = LaserCutAnalysis(
        equipment=equipment,
        user=user,
        batch=batch,
        sequence=sequence,
        material=material,
        material_code_snapshot=material.code if material else "",
        sheet_rate_snapshot=material.sheet_rate if material else None,
        original_filename=f"{name}.dxf",
        part_name=name,
        quantity=quantity,
        status=PrintAnalysisStatus.COMPLETED,
        detected_units="mm",
        units="mm",
        bbox_drawing_units={"min_x": 0, "min_y": 0, "max_x": float(w), "max_y": float(h)},
        width_mm=w,
        height_mm=h,
        area_mm2=w * h,
        entity_count=1,
        booking=booking,
    )
    part.dxf_file.save(f"{name}.dxf", ContentFile(data or dxf_bytes(rects=((0, 0, float(w), float(h)),))), save=False)
    part.save()
    return part


def print_material(equipment, *, code="PLA-FDM", price="1.4400", name="PLA (FDM)"):
    from iic_booking.equipment.models import PrintMaterial

    return PrintMaterial.objects.create(
        equipment=equipment,
        code=code,
        name=name,
        density_g_per_cm3=Decimal("1.24"),
        price_per_gram=Decimal(price),
    )


def print_part(equipment, user, material, *, weight="10.2", minutes=30, quantity=1, name="gear", batch=None,
               booking=None, sequence=0):
    from iic_booking.equipment.models import PrintAnalysis, PrintAnalysisStatus

    part = PrintAnalysis(
        equipment=equipment,
        user=user,
        batch=batch,
        sequence=sequence,
        material=material,
        material_code_snapshot=material.code,
        price_per_gram_snapshot=material.price_per_gram,
        original_filename=f"{name}.stl",
        part_name=name,
        quantity=quantity,
        status=PrintAnalysisStatus.COMPLETED,
        weight_grams=Decimal(weight),
        estimated_time_minutes=minutes,
        booking=booking,
    )
    part.stl_file.save(f"{name}.stl", ContentFile(b"solid gear\nendsolid gear\n"), save=False)
    part.save()
    return part


def funded_student(egs_factory, balance="10000.00"):
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.repositories.wallet_repository import SubWalletRepository
    from iic_booking.users.tests.factories import UserFactory

    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    sub = SubWalletRepository.get_or_create(wallet, egs_factory.department)
    sub.credit(Decimal(balance), description="Recharge")
    return student, sub
