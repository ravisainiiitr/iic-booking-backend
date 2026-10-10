"""Booking detail preview of booked STL / DXF files: who may load the files and what the preview is given."""

from __future__ import annotations

import pytest

from iic_booking.equipment.fabrication import fabrication_parts_summary
from iic_booking.equipment.models import Booking, EquipmentManager, EquipmentOperator, LaserCutBatch
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import (
    acrylic_3mm,
    funded_student,
    internal_department,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
    print_part,
)


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


def _staff(egs_factory, user_type, department=None):
    return UserFactory(user_type=user_type, department=department or egs_factory.department, admin_approved=True)


def _get(egs_factory, user, url):
    return egs_factory.client_for(user).get(url)


@pytest.mark.django_db
def test_booked_dxf_streams_for_everyone_who_sees_the_booking(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="202.00")
    batch = LaserCutBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    part = laser_part(eq, student, acr, batch=batch, booking=booking)
    url = f"/api/laser-cut-analyses/{part.pk}/dxf/"

    oic = _staff(egs_factory, UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    operator = _staff(egs_factory, UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    dept_admin = _staff(egs_factory, UserType.DEPT_ADMIN)
    wallet_faculty = WalletJoinRequest.objects.get(student=student).faculty

    for viewer in (student, oic, operator, dept_admin, wallet_faculty):
        resp = _get(egs_factory, viewer, url)
        assert resp.status_code == 200, (viewer.user_type, getattr(resp, "data", None))

    other_dept_admin = _staff(egs_factory, UserType.DEPT_ADMIN, department=internal_department())
    finance = _staff(egs_factory, UserType.FINANCE)
    for outsider in (egs_factory.student(), other_dept_admin, finance):
        assert _get(egs_factory, outsider, url).status_code == 404, outsider.user_type


@pytest.mark.django_db
def test_booked_stl_streams_for_lab_staff_and_owner_of_old_single_file_booking(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    operator = _staff(egs_factory, UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="15.00")
    # Old bookings point at their single STL from the booking; the file was uploaded by the operator.
    analysis = print_part(eq, operator, pla)
    Booking.objects.filter(pk=booking.pk).update(print_analysis=analysis)
    url = f"/api/print-analyses/{analysis.pk}/stl/"

    oic = _staff(egs_factory, UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    for viewer in (student, operator, oic):
        assert _get(egs_factory, viewer, url).status_code == 200, viewer.user_type
    assert _get(egs_factory, egs_factory.student(), url).status_code == 404


@pytest.mark.django_db
def test_booking_detail_parts_carry_what_the_preview_draws(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    laser_booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="202.00")
    laser_part(eq, student, acr, booking=laser_booking)

    [laser] = BookingSerializer(laser_booking, context={"request": None}).data["fabrication_parts"]
    assert laser["material_family"] == "ACRYLIC"
    # Job sheets, emails and file-change history keep their rows unchanged.
    assert "material_family" not in fabrication_parts_summary(laser_booking)[0]

    peq = print_equipment(egs_factory)
    pla = print_material(peq)
    print_booking = egs_factory.booking(student, peq, egs_factory.future(days=3), total_charge="15.00")
    model = print_part(peq, student, pla, booking=print_booking)
    model.slicer_settings = {"layer_height_mm": 0.12}
    model.bounding_box = {"_estimate": {"total_min": 42.5, "warmup_min": 6.0, "progress": [0.25, 0.5, 1.0]}}
    model.save(update_fields=["slicer_settings", "bounding_box"])

    [printed] = BookingSerializer(print_booking, context={"request": None}).data["fabrication_parts"]
    assert printed["layer_height_mm"] == 0.12
    assert printed["print_progress"] == [0.25, 0.5, 1.0]
    assert printed["print_minutes"] == 42.5
    assert printed["warmup_minutes"] == 6.0
    assert "print_progress" not in fabrication_parts_summary(print_booking)[0]
