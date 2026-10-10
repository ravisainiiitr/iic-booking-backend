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
    assert printed["file_available"] is True
    assert printed["layer_height_mm"] == 0.12
    assert printed["print_progress"] == [0.25, 0.5, 1.0]
    assert printed["print_minutes"] == 42.5
    assert printed["warmup_minutes"] == 6.0
    assert "print_progress" not in fabrication_parts_summary(print_booking)[0]


@pytest.mark.django_db
def test_multi_file_print_booking_streams_each_stl_until_completion_removes_them(egs_factory, media_tmp):
    from iic_booking.equipment.models import BookingStatus, PrintAnalysisBatch
    from iic_booking.equipment.print_3d_notifications import delete_print_3d_booking_stl_files

    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="30.00")
    batch = PrintAnalysisBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    gear = print_part(eq, student, pla, name="gear", batch=batch, booking=booking, sequence=0)
    hub = print_part(eq, student, pla, name="hub", batch=batch, booking=booking, sequence=1)
    oic = _staff(egs_factory, UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    operator = _staff(egs_factory, UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)

    parts = BookingSerializer(booking, context={"request": None}).data["fabrication_parts"]
    assert [(p["analysis_id"], p["file_available"]) for p in parts] == [(str(gear.pk), True), (str(hub.pk), True)]
    for viewer in (student, oic, operator):
        for analysis in (gear, hub):
            resp = _get(egs_factory, viewer, f"/api/print-analyses/{analysis.pk}/stl/")
            assert resp.status_code == 200, viewer.user_type
            assert resp.content == b"solid gear\nendsolid gear\n"

    Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.COMPLETED)
    assert delete_print_3d_booking_stl_files(booking.pk) == 2
    booking.refresh_from_db()
    parts = BookingSerializer(booking, context={"request": None}).data["fabrication_parts"]
    assert [p["file_available"] for p in parts] == [False, False]
    assert _get(egs_factory, student, f"/api/print-analyses/{gear.pk}/stl/").status_code == 404


def _view_booking_parts(egs_factory, viewer, booking):
    """The booking View Booking / My Bookings opens: the full booking fetched by its id, as the page does."""
    resp = _get(egs_factory, viewer, f"/api/bookings/?booking_id={booking.pk}&limit=1")
    assert resp.status_code == 200, (viewer.user_type, getattr(resp, "data", None))
    [row] = resp.json()["bookings"]
    assert row["equipment_profile_type"] == "PRINT_3D", viewer.user_type
    return row["fabrication_parts"]


@pytest.mark.django_db
def test_view_booking_of_test_printer_booking_previews_every_stl_for_owner_and_staff(egs_factory, media_tmp):
    """Shaped like IICTEST-3DP-01202600002: the test-only printer TEST-3DP-01, two STLs uploaded together (one
    turned on the plate), and an older booking that points at its single STL from the booking only."""
    from iic_booking.equipment.models import PrintAnalysis, PrintAnalysisBatch

    eq = print_equipment(egs_factory, code="TEST-3DP-01", visible_to_test_accounts_only=True)
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    student.is_test_account = True
    student.save(update_fields=["is_test_account"])
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="30.00")
    Booking.objects.filter(pk=booking.pk).update(virtual_booking_id="IICTEST-3DP-01202600002")
    batch = PrintAnalysisBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    flipped = [1, 0, 0, 0, -1, 0, 0, 0, -1]
    gear = print_part(eq, student, pla, name="gear", batch=batch, booking=booking, sequence=0)
    hub = print_part(eq, student, pla, name="hub", batch=batch, booking=booking, sequence=1)
    PrintAnalysis.objects.filter(pk=gear.pk).update(volume_cm3="8.2346", slicer_settings={"orientation": flipped})
    Booking.objects.filter(pk=booking.pk).update(print_analysis=gear, print_analysis_batch=batch)

    legacy = egs_factory.booking(student, eq, egs_factory.future(days=4), total_charge="15.00")
    old_model = print_part(eq, student, pla, name="bracket")
    Booking.objects.filter(pk=legacy.pk).update(print_analysis=old_model)

    oic = _staff(egs_factory, UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    operator = _staff(egs_factory, UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    main_admin = _staff(egs_factory, UserType.ADMIN)

    for viewer in (student, oic, operator, main_admin):
        parts = _view_booking_parts(egs_factory, viewer, booking)
        assert [(p["analysis_id"], p["file_available"]) for p in parts] == [(str(gear.pk), True), (str(hub.pk), True)]
        assert parts[0]["orientation"] == flipped
        assert parts[0]["volume_cm3"] == 8.23
        assert (parts[0]["weight_g_each"], parts[0]["time_min_each"]) == (11, 30)
        for part in parts:
            resp = _get(egs_factory, viewer, f"/api/print-analyses/{part['analysis_id']}/stl/")
            assert resp.status_code == 200, (viewer.user_type, part["name"])
            assert resp.content == b"solid gear\nendsolid gear\n"

        [old] = _view_booking_parts(egs_factory, viewer, legacy)
        assert (old["analysis_id"], old["file_available"]) == (str(old_model.pk), True)
        assert _get(egs_factory, viewer, f"/api/print-analyses/{old_model.pk}/stl/").status_code == 200
