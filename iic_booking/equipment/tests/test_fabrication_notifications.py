"""Lab inbox emails for fabrication bookings, file cleanup on completion, and the email-list data migration."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps
from django.core import mail

from iic_booking.equipment.models import BookingStatus, Equipment, LaserCutAnalysis
from iic_booking.equipment.print_3d_notifications import (
    REASON_FILES_RESTORED,
    REASON_FILES_UPDATED,
    delete_print_3d_booking_stl_files,
    notification_recipients,
    send_print_3d_stl_booking_email,
)

from .fabrication_helpers import (
    acrylic_3mm,
    funded_student,
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
    settings.FRONTEND_URL = "https://portal.test"
    return tmp_path


def _laser_booking(egs_factory, *, emails=("lab@iitr.ac.in", "LAB@iitr.ac.in", "jobs@iitr.ac.in"), own=False):
    eq = laser_equipment(egs_factory, own_charge="250", emails=emails)
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="202.00", own_material=own)
    a = laser_part(eq, student, acr, quantity=5, name="bracket", booking=booking, sequence=0)
    b = laser_part(eq, student, acr, width="50", height="50", name="washer", booking=booking, sequence=1)
    return eq, booking, a, b


def test_recipients_are_deduplicated_case_insensitively():
    eq = Equipment(fabrication_notification_emails=["a@x.in", " A@x.in ", "", "b@x.in"])
    assert notification_recipients(eq) == ["a@x.in", "b@x.in"]


@pytest.mark.django_db
def test_laser_booking_email_attaches_dxfs_and_lists_parts(egs_factory, media_tmp):
    _eq, booking, _a, _b = _laser_booking(egs_factory, own=True)
    assert send_print_3d_stl_booking_email(booking.pk) is True
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["lab@iitr.ac.in", "jobs@iitr.ac.in"]
    assert msg.subject.startswith("Laser cutting booking ")
    assert sorted(name for name, _content, _mime in msg.attachments) == ["bracket.dxf", "washer.dxf"]
    assert "bracket × 5 — Acrylic sheet 3 mm, 200.0 × 100.0 mm (0.0200 m² each) [bracket.dxf]" in msg.body
    assert "Material: user brings own material" in msg.body
    assert "2 DXF file(s) attached" in msg.body


@pytest.mark.django_db
def test_files_over_the_attachment_cap_are_sent_as_links(egs_factory, media_tmp, settings):
    _eq, booking, a, _b = _laser_booking(egs_factory)
    settings.FABRICATION_EMAIL_MAX_ATTACHMENT_BYTES = a.dxf_file.size  # room for the first file only
    send_print_3d_stl_booking_email(booking.pk, reason=REASON_FILES_UPDATED)
    msg = mail.outbox[0]
    assert msg.subject.startswith("UPDATED FILES — Laser cutting booking")
    assert [name for name, _c, _m in msg.attachments] == ["bracket.dxf"]
    assert f"washer.dxf: https://portal.test/bookings/{booking.pk}" in msg.body
    assert "too large to attach" in msg.body
    assert msg.body.startswith("UPDATED: the design files")


@pytest.mark.django_db
def test_restored_email_and_no_recipients(egs_factory, media_tmp):
    _eq, booking, _a, _b = _laser_booking(egs_factory)
    send_print_3d_stl_booking_email(booking.pk, reason=REASON_FILES_RESTORED)
    assert "replacement on this 2D laser cutting booking was cancelled" in mail.outbox[0].body

    _eq2, quiet, _c, _d = _laser_booking(egs_factory, emails=())
    assert send_print_3d_stl_booking_email(quiet.pk) is False
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_print_email_uses_quantities(egs_factory, media_tmp):
    eq = print_equipment(egs_factory, emails=["print@iitr.ac.in"])
    pla = print_material(eq)
    student, _sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future())
    print_part(eq, student, pla, weight="10", minutes=30, quantity=3, name="gear", booking=booking)
    send_print_3d_stl_booking_email(booking.pk)
    msg = mail.outbox[0]
    assert msg.subject.startswith("3D print booking ")
    assert "gear × 3, est. 10 g / 30 min each [gear.stl]" in msg.body
    assert [name for name, _c, _m in msg.attachments] == ["gear.stl"]


@pytest.mark.django_db
def test_completed_booking_deletes_current_and_replaced_dxfs(egs_factory, media_tmp):
    eq, booking, a, b = _laser_booking(egs_factory)
    acr = a.material
    replaced = laser_part(eq, booking.user, acr, name="old")
    LaserCutAnalysis.objects.filter(pk=replaced.pk).update(superseded_booking=booking)
    paths = [part.dxf_file.path for part in (a, b, replaced)]

    assert delete_print_3d_booking_stl_files(booking.pk) == 0  # still BOOKED

    booking.status = BookingStatus.COMPLETED
    booking.save(update_fields=["status"])
    assert delete_print_3d_booking_stl_files(booking.pk) == 3
    import os

    assert not any(os.path.exists(p) for p in paths)
    assert set(LaserCutAnalysis.objects.filter(pk__in=[a.pk, b.pk, replaced.pk]).values_list("dxf_file", flat=True)) == {""}


@pytest.mark.django_db
def test_email_list_migration_forward_and_back(egs_factory):
    migration = importlib.import_module("iic_booking.equipment.migrations.0229_fabrication_notification_emails_data")
    single = egs_factory.equipment(print_3d_stl_notification_email="jobwork.tl@iitr.ac.in")
    merged = egs_factory.equipment(
        print_3d_stl_notification_email="Jobwork.TL@iitr.ac.in", fabrication_notification_emails=["jobwork.tl@iitr.ac.in"]
    )
    blank = egs_factory.equipment()

    migration.copy_single_email_into_list(django_apps, None)
    for eq in (single, merged, blank):
        eq.refresh_from_db()
    assert single.fabrication_notification_emails == ["jobwork.tl@iitr.ac.in"]
    assert merged.fabrication_notification_emails == ["jobwork.tl@iitr.ac.in"]
    assert blank.fabrication_notification_emails == []

    Equipment.objects.filter(pk=single.pk).update(print_3d_stl_notification_email="")
    Equipment.objects.filter(pk=blank.pk).update(fabrication_notification_emails=["new@iitr.ac.in", "x@iitr.ac.in"])
    migration.copy_first_list_email_back(django_apps, None)
    single.refresh_from_db()
    blank.refresh_from_db()
    assert single.print_3d_stl_notification_email == "jobwork.tl@iitr.ac.in"
    assert blank.print_3d_stl_notification_email == "new@iitr.ac.in"
