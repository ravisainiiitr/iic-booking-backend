"""Maximum print size of 3D printers: STL bounding box, the fit rule, settings and server enforcement."""

from __future__ import annotations

import io
import struct
import zipfile
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from iic_booking.equipment.models import Booking, EquipmentManager, PrintAnalysis, PrintAnalysisBatch
from iic_booking.equipment.print_size_limit import (
    TOLERANCE_MM,
    PrintSizeLimit,
    equipment_print_size_limit,
    fits_only_when_rotated,
    fits_print_size,
    print_size_error,
    stl_bounding_box_size,
    tiny_model_warning,
)
from iic_booking.equipment.serializers import EquipmentDetailSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import funded_student, laser_equipment, print_equipment, print_material, print_part

BOX_FACES = (
    ((0, 0, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
    ((0, 0, 1), (1, 0, 1), (1, 1, 1)), ((0, 0, 1), (1, 1, 1), (0, 1, 1)),
    ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (0, 0, 1)),
    ((0, 1, 0), (1, 1, 1), (1, 1, 0)), ((0, 1, 0), (0, 1, 1), (1, 1, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 1, 0)),
    ((1, 0, 0), (1, 1, 0), (1, 1, 1)), ((1, 0, 0), (1, 1, 1), (1, 0, 1)),
)


def _box_triangles(w, d, h, offset=(5.0, -3.0, 2.0)):
    ox, oy, oz = offset
    return [[(ox + x * w, oy + y * d, oz + z * h) for x, y, z in tri] for tri in BOX_FACES]


def binary_box_stl(w, d, h, header=b"binary box") -> bytes:
    tris = _box_triangles(w, d, h)
    out = bytearray(header.ljust(80, b" ")[:80])
    out += struct.pack("<I", len(tris))
    for tri in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for v in tri:
            out += struct.pack("<3f", *v)
        out += struct.pack("<H", 0)
    return bytes(out)


def ascii_box_stl(w, d, h) -> bytes:
    lines = ["solid box"]
    for tri in _box_triangles(w, d, h):
        lines += ["  facet normal 0 0 0", "    outer loop"]
        lines += [f"      vertex {x:.6e} {y:.6f} {z:g}" for x, y, z in tri]
        lines += ["    endloop", "  endfacet"]
    lines.append("endsolid box")
    return "\n".join(lines).encode()


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    settings.PRINT_3D_USE_CELERY = False
    settings.PRINT_3D_ASYNC_INLINE = False
    settings.CURAENGINE_PATH = ""
    return tmp_path


def _limit(x, y, z, rotate=True):
    return PrintSizeLimit(x, y, z, allow_rotation=rotate)


# --------------------------------------------------------------------------- bounding box


@pytest.mark.parametrize("builder", [binary_box_stl, ascii_box_stl])
def test_bounding_box_of_binary_and_ascii_stl(builder):
    size = stl_bounding_box_size(builder(120.5, 40, 7.25))
    assert size == pytest.approx((120.5, 40, 7.25), abs=1e-3)


def test_binary_stl_whose_header_starts_with_solid_is_read_as_binary():
    data = binary_box_stl(10, 20, 30, header=b"solid exported by CAD facet normal vertex")
    assert stl_bounding_box_size(data) == pytest.approx((10, 20, 30), abs=1e-4)


def test_empty_stl_is_an_error():
    with pytest.raises(ValueError):
        stl_bounding_box_size(b"solid x\nendsolid x\n")


# --------------------------------------------------------------------------- fit rule


def test_no_limit_always_fits():
    assert fits_print_size((5000, 5000, 5000), None)
    assert print_size_error("a.stl", (5000, 5000, 5000), None) is None


def test_fit_without_rotation_is_per_axis_with_tolerance():
    limit = _limit(200, 100, 50, rotate=False)
    assert fits_print_size((200, 100, 50), limit)
    assert fits_print_size((200 + TOLERANCE_MM, 100, 50), limit)
    assert not fits_print_size((200.6, 100, 50), limit)
    assert not fits_print_size((100, 200, 50), limit)  # would fit turned, but rotation is off


def test_fit_with_rotation_compares_sorted_dimensions():
    limit = _limit(200, 100, 50)
    assert fits_print_size((50, 200, 100), limit)
    assert fits_only_when_rotated((50, 200, 100), limit)
    assert not fits_only_when_rotated((200, 100, 50), limit)
    assert fits_print_size((100.5, 50.5, 200.5), limit)
    assert not fits_print_size((150, 150, 10), limit)  # two sides over 100 cannot fit any way round
    message = print_size_error("big.stl", (150, 150, 10), limit)
    assert "big.stl is 150 × 150 × 10 mm" in message
    assert "200 × 100 × 50 mm even when rotated" in message


def test_blank_axis_has_no_limit():
    assert fits_print_size((10, 10, 900), _limit(20, 20, None, rotate=False))
    assert not fits_print_size((30, 10, 900), _limit(20, 20, None, rotate=False))
    assert fits_print_size((900, 10, 10), _limit(20, 20, None))


def test_tiny_model_warns_about_units():
    assert "millimetres" in tiny_model_warning((0.2, 0.1, 0.05))
    assert tiny_model_warning((1.0, 0.1, 0.1)) is None


@pytest.mark.django_db
def test_equipment_limit_null_means_no_limit_and_rotation_defaults_on(egs_factory):
    eq = print_equipment(egs_factory)
    assert equipment_print_size_limit(eq) is None
    eq.max_print_size_x_mm = Decimal("220")
    eq.max_print_size_z_mm = Decimal("250")
    eq.allow_print_rotation_to_fit = None
    limit = equipment_print_size_limit(eq)
    assert limit == PrintSizeLimit(220.0, None, 250.0, allow_rotation=True)
    eq.allow_print_rotation_to_fit = False
    assert equipment_print_size_limit(eq).allow_rotation is False


# --------------------------------------------------------------------------- settings and APIs


@pytest.mark.django_db
def test_oic_sets_max_print_size_on_the_materials_page(egs_factory):
    eq = print_equipment(egs_factory)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    client = egs_factory.client_for(oic)
    url = "/api/oic/fabrication-materials/equipment/"

    for bad in ("abc", 0, -5, 20000):
        resp = client.patch(url, {"equipment_id": eq.pk, "max_print_size_x_mm": bad}, format="json")
        assert resp.status_code == 400, bad

    resp = client.patch(
        url,
        {
            "equipment_id": eq.pk,
            "max_print_size_x_mm": "220",
            "max_print_size_y_mm": 220.04,
            "max_print_size_z_mm": "",
            "allow_print_rotation_to_fit": False,
        },
        format="json",
    )
    assert resp.status_code == 200, resp.data
    row = resp.data["equipment"]
    assert (row["max_print_size_x_mm"], row["max_print_size_y_mm"], row["max_print_size_z_mm"]) == ("220.0", "220.0", None)
    assert row["allow_print_rotation_to_fit"] is False
    eq.refresh_from_db()
    assert eq.max_print_size_x_mm == Decimal("220.0") and eq.max_print_size_z_mm is None

    detail = EquipmentDetailSerializer().get_max_print_size(eq)
    assert detail == {"x": 220.0, "y": 220.0, "z": None, "allow_rotation": False, "tolerance_mm": TOLERANCE_MM}
    resp = egs_factory.client_for(egs_factory.student()).get(f"/api/equipments/{eq.pk}/print-materials/")
    assert resp.status_code == 200
    assert resp.data["max_print_size"]["x"] == 220.0

    laser = laser_equipment(egs_factory)
    EquipmentManager.objects.create(equipment=laser, manager=oic)
    resp = client.patch(url, {"equipment_id": laser.pk, "max_print_size_x_mm": "100"}, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_admin_sees_and_sets_size_for_any_printer(egs_factory):
    eq = print_equipment(egs_factory)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    client = egs_factory.client_for(admin)
    url = "/api/oic/fabrication-materials/equipment/"
    rows = {r["equipment_id"]: r for r in client.get(url).data["equipments"]}
    assert rows[eq.pk]["max_print_size_x_mm"] is None and rows[eq.pk]["allow_print_rotation_to_fit"] is True
    resp = client.patch(url, {"equipment_id": eq.pk, "max_print_size_z_mm": "180"}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["equipment"]["max_print_size_z_mm"] == "180.0"


def _analyze(client, eq, name, data, material):
    content_type = "application/zip" if name.endswith(".zip") else "model/stl"
    return client.post(
        f"/api/equipments/{eq.pk}/analyze-stl/",
        {"file": SimpleUploadedFile(name, data, content_type=content_type), "material_id": material.pk},
        format="multipart",
    )


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files:
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.django_db
def test_upload_larger_than_the_printer_is_rejected(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)

    # No limit: a large model is accepted as before.
    resp = _analyze(client, eq, "big.stl", binary_box_stl(300, 20, 20), pla)
    assert resp.status_code == 200, resp.data

    eq.max_print_size_x_mm, eq.max_print_size_y_mm, eq.max_print_size_z_mm = Decimal(200), Decimal(100), Decimal(50)
    eq.allow_print_rotation_to_fit = False
    eq.save()
    before = PrintAnalysis.objects.count()
    resp = _analyze(client, eq, "big.stl", binary_box_stl(300, 20, 20), pla)
    assert resp.status_code == 400
    assert resp.data["code"] == "PRINT_SIZE_EXCEEDED"
    assert "big.stl is 300 × 20 × 20 mm" in resp.data["error"]
    assert "200 × 100 × 50 mm" in resp.data["error"]
    assert resp.data["max_print_size"]["allow_rotation"] is False
    assert PrintAnalysis.objects.count() == before

    # Turned on its side it would fit, but rotation is off for this printer.
    resp = _analyze(client, eq, "side.stl", ascii_box_stl(40, 180, 10), pla)
    assert resp.status_code == 400
    eq.allow_print_rotation_to_fit = True
    eq.save(update_fields=["allow_print_rotation_to_fit"])
    resp = _analyze(client, eq, "side.stl", ascii_box_stl(40, 180, 10), pla)
    assert resp.status_code == 200, resp.data
    assert resp.data["status"] == "COMPLETED"

    # Within the 0.5 mm tolerance.
    assert _analyze(client, eq, "edge.stl", binary_box_stl(200.4, 100, 50), pla).status_code == 200


@pytest.mark.django_db
def test_zip_upload_checks_every_file(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    eq.max_print_size_x_mm = eq.max_print_size_y_mm = eq.max_print_size_z_mm = Decimal(100)
    eq.save()
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    data = _zip([("ok.stl", binary_box_stl(50, 50, 50)), ("wide.stl", binary_box_stl(150, 10, 10)),
                 ("tall.stl", ascii_box_stl(10, 10, 101))])
    batches = PrintAnalysisBatch.objects.count()
    resp = _analyze(client, eq, "parts.zip", data, pla)
    assert resp.status_code == 400
    assert [f["filename"] for f in resp.data["too_large_files"]] == ["wide.stl", "tall.stl"]
    assert resp.data["error"].startswith("2 STL files are larger")
    assert PrintAnalysisBatch.objects.count() == batches


def _oversize_part(eq, user, material, name="huge", **kwargs):
    part = print_part(eq, user, material, name=name, **kwargs)
    part.bounding_box = {"size": {"x": 260.0, "y": 30.0, "z": 30.0}}
    part.save(update_fields=["bounding_box"])
    return part


@pytest.mark.django_db
def test_booking_and_file_replacement_recheck_the_size(egs_factory, media_tmp, monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    # Uploaded before the OIC set the limit.
    huge = _oversize_part(eq, student, pla)
    eq.max_print_size_x_mm = eq.max_print_size_y_mm = eq.max_print_size_z_mm = Decimal(220)
    eq.save()

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
        "print_analysis_id": str(huge.id),
    }
    resp = client.post(f"/api/equipments/{eq.pk}/book/", body, format="json")
    assert resp.status_code == 400
    assert "larger than this printer's maximum print size" in str(resp.data)
    assert not Booking.objects.filter(user=student, equipment=eq).exists()

    booking = egs_factory.booking(student, eq, egs_factory.future(), slot_count=2, total_charge="15.00")
    old = print_part(eq, student, pla, weight="10", minutes=30, name="old", booking=booking)
    booking.print_analysis = old
    booking.input_values = {"A": 10, "B": "PLA-FDM", "C": 30}
    booking.save(update_fields=["print_analysis", "input_values"])
    now = timezone.now()
    Booking.objects.filter(pk=booking.pk).update(
        fabrication_rejected_at=now, fabrication_replace_deadline=now + timedelta(hours=24),
        fabrication_rejection_reason="Too thin.",
    )
    batch = PrintAnalysisBatch.objects.create(equipment=eq, user=student, material=pla, status="COMPLETED")
    print_part(eq, student, pla, name="fine", batch=batch)
    _oversize_part(eq, student, pla, name="wide", batch=batch, sequence=1)
    resp = client.post(
        f"/api/bookings/{booking.pk}/fabrication-files/", {"print_analysis_batch_id": str(batch.id)}, format="json"
    )
    assert resp.status_code == 400
    assert "wide.stl is 260 × 30 × 30 mm" in resp.data["error"]
    old.refresh_from_db()
    assert old.booking_id == booking.pk
