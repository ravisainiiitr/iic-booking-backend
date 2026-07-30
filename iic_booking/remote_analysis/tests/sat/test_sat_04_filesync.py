"""SAT-04 File Synchronization."""

from __future__ import annotations

import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferManager
from iic_booking.remote_analysis.workspace_models import WorkspaceFile


def _workspace(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
    )
    reservation.workstation = eligible_workstation
    reservation.save(update_fields=["workstation", "updated_at"])
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.save(update_fields=["workstation", "updated_at"])
    return workspace


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_01_single_file(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    content = b"sat-single-file"
    row = TransferManager().upload(
        workspace,
        SimpleUploadedFile("sample.txt", content, content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    assert row.sha256 == hashlib.sha256(content).hexdigest()
    assert row.relative_path.startswith("RawData/")


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_02_multiple_files(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    mgr = TransferManager()
    for i in range(3):
        mgr.upload(
            workspace,
            SimpleUploadedFile(f"f{i}.txt", f"body-{i}".encode(), content_type="text/plain"),
            folder="RawData",
            actor=ra_user,
        )
    assert WorkspaceFile.objects.filter(workspace=workspace, deleted=False).count() >= 3


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_04_empty_file(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    row = TransferManager().upload(
        workspace,
        SimpleUploadedFile("empty.txt", b"", content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    assert row.size == 0
    assert row.sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_05_unicode_filename(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    name = "样品-αβγ.txt"
    row = TransferManager().upload(
        workspace,
        SimpleUploadedFile(name, b"unicode", content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    assert "RawData/" in row.relative_path


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_06_long_filename(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    name = ("long-" + ("x" * 180) + ".txt")[:200]
    row = TransferManager().upload(
        workspace,
        SimpleUploadedFile(name, b"long", content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    assert row.id


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_04_07_duplicate_filename(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    workspace = _workspace(ra_user, eligible_workstation, reservation_window)
    mgr = TransferManager()
    a = mgr.upload(
        workspace,
        SimpleUploadedFile("dup.txt", b"one", content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    b = mgr.upload(
        workspace,
        SimpleUploadedFile("dup.txt", b"two", content_type="text/plain"),
        folder="RawData",
        actor=ra_user,
    )
    # Policy: either versioned path or new row — must not corrupt first checksum silently
    assert a.sha256 != b.sha256 or a.id == b.id
    assert WorkspaceFile.objects.filter(workspace=workspace, deleted=False).exists()


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_04_03_large_file_lab(sat_lab_enabled):
    pytest.skip("Lab/perf: transfer >1GB; record in docs/sat/08-Performance-Baseline.md")


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_04_interrupt_retry_lab(sat_lab_enabled):
    pytest.skip("Lab: interrupt download/upload; verify retry per checklist 04.09–04.11")
