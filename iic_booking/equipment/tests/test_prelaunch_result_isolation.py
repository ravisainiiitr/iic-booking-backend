"""Results never leak from an earlier booking that had the same display ID, and the pre-launch purge is
reversible, idempotent and limited to booking result data."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import prelaunch_purge as purge
from iic_booking.equipment.models import Booking, BookingResultFile, BookingStatus, ChargeProfile, Equipment
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

AUGUST = datetime(2026, 8, 10, 6, 0, tzinfo=dt_timezone.utc)


class FakeS3:
    """In-memory S3 with the calls the results code and the purge use."""

    def __init__(self, objects: dict[str, tuple[bytes, datetime]] | None = None):
        self.objects: dict[str, tuple[bytes, datetime]] = dict(objects or {})

    def get_paginator(self, _name):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix=""):
                contents = [
                    {"Key": k, "LastModified": t, "Size": len(b), "ETag": '"e"'}
                    for k, (b, t) in sorted(fake.objects.items())
                    if k.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _P()

    def generate_presigned_url(self, _op, Params, ExpiresIn=3600):
        return f"https://s3.test/{Params['Key']}"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key][0])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def copy(self, CopySource, Bucket, Key):
        body, _t = self.objects[CopySource["Key"]]
        self.objects[Key] = (body, timezone.now())

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def put_object(self, Bucket, Key, Body, ContentType=""):
        self.objects[Key] = (Body, timezone.now())


def _booking(user, *, vid: str | None = None, status=BookingStatus.COMPLETED):
    equipment = Equipment.objects.create(
        name="PXRD", code=f"PX{uuid.uuid4().hex[:4].upper()}", slot_duration_minutes=60, user_rating_enabled=False
    )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    return Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=vid or f"IIC{equipment.code}202600001",
    )


@pytest.fixture
def fake_s3(monkeypatch, settings):
    settings.AWS_STORAGE_BUCKET_NAME = "test-bucket"
    fake = FakeS3()
    import boto3

    import iic_booking.sync.services.results_s3 as results_s3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    monkeypatch.setattr(results_s3, "_s3_client", lambda: (fake, "test-bucket"))
    cache.clear()
    return fake


@pytest.mark.django_db
def test_reused_display_id_does_not_show_older_s3_results(fake_s3, tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    user = UserFactory()
    booking = _booking(user)
    vid = booking.virtual_booking_id
    fake_s3.objects[f"Results/{vid}/old-test.csv"] = (b"old", AUGUST)
    fake_s3.objects[f"Results/Lab/2026/{vid}/old-nested.csv"] = (b"old2", AUGUST)
    fake_s3.objects[f"Results/{vid}/genuine.csv"] = (b"new", booking.created_at + timedelta(minutes=5))

    client = APIClient()
    client.force_authenticate(user=user)
    listed = client.get(f"/api/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    assert [f["name"] for f in listed.json()["files"]] == ["genuine.csv"]

    zipped = client.get(f"/api/bookings/{booking.pk}/results/download/")
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
        assert [m.rsplit("/", 1)[-1] for m in zf.namelist()] == ["genuine.csv"]


@pytest.mark.django_db
def test_only_older_s3_results_means_no_results_and_no_notification(fake_s3):
    from iic_booking.equipment.api_views import _notify_user_results_available_by_id

    user = UserFactory()
    booking = _booking(user)
    fake_s3.objects[f"Results/{booking.virtual_booking_id}/old-test.csv"] = (b"old", AUGUST)

    client = APIClient()
    client.force_authenticate(user=user)
    body = client.get(f"/api/bookings/{booking.pk}/results/").json()
    assert body["exists"] is False and body["files"] == []

    _notify_user_results_available_by_id(booking.pk)
    booking.refresh_from_db()
    assert booking.results_available_notified_at is None


@pytest.mark.django_db
def test_remote_analysis_staging_ignores_older_s3_results(fake_s3):
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import booking_file_count
    from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService

    booking = _booking(UserFactory())
    fake_s3.objects[f"Results/{booking.virtual_booking_id}/old-test.raw"] = (b"old", AUGUST)
    staging = BookingRawStagingService()
    assert staging.list_raw_entries(booking, fresh=True) == []
    assert staging.has_raw_files(booking) is False
    assert booking_file_count(booking) == 0

    fake_s3.objects[f"Results/{booking.virtual_booking_id}/new.raw"] = (b"new", booking.created_at + timedelta(minutes=1))
    assert [e["name"] for e in staging.list_raw_entries(booking, fresh=True)] == ["new.raw"]


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_db_result_rows_older_than_the_booking_are_hidden(tmp_path, settings):
    """Defence in depth for a reused pk: rows created before the booking record are never its results."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    user = UserFactory()
    booking = _booking(user)
    old = BookingResultFile.objects.create(
        booking=booking, file=SimpleUploadedFile("old.pdf", b"old"), original_name="old.pdf"
    )
    BookingResultFile.objects.filter(pk=old.pk).update(created_at=booking.created_at - timedelta(days=30))
    BookingResultFile.objects.create(booking=booking, file=SimpleUploadedFile("new.pdf", b"new"), original_name="new.pdf")

    client = APIClient()
    client.force_authenticate(user=user)
    names = [f["name"] for f in client.get(f"/api/bookings/{booking.pk}/results/").json()["files"]]
    assert names == ["new.pdf"]
    assert client.get(f"/api/bookings/{booking.pk}/results/files/{old.pk}/").status_code == 404

    from iic_booking.equipment.booking_results_service import booking_has_results_annotation

    BookingResultFile.objects.filter(booking=booking).exclude(pk=old.pk).delete()
    annotated = Booking.objects.filter(pk=booking.pk).annotate(has=booking_has_results_annotation()).get()
    assert annotated.has is False


def test_new_dsa_result_keys_are_unique_per_booking_record():
    from types import SimpleNamespace

    from iic_booking.sync.services.results_s3 import booking_results_folder, build_results_s3_key

    created = datetime(2026, 10, 1, 9, 30, 5, tzinfo=dt_timezone.utc)
    a = SimpleNamespace(pk=566, created_at=created)
    b = SimpleNamespace(pk=901, created_at=created + timedelta(days=400))
    key_a = build_results_s3_key("IICPXRD[A]202600001", "x.csv", booking_folder=booking_results_folder(a))
    key_b = build_results_s3_key("IICPXRD[A]202600001", "x.csv", booking_folder=booking_results_folder(b))
    assert key_a == "Results/IICPXRD[A]202600001/b566-20261001093005/x.csv"
    assert key_a != key_b


def _purge_objects(booking, referenced_name):
    vid = booking.virtual_booking_id
    after = booking.created_at + timedelta(minutes=1)
    return {
        f"Results/{vid}/old.csv": (b"old", AUGUST),
        "Results/IICXRD202600009/orphan.csv": (b"orphan", AUGUST),
        f"Results/{vid}/genuine.csv": (b"new", after),
        "media/booking_results/2026/08/10/orphan.pdf": (b"orph", AUGUST),
        f"media/{referenced_name}": (b"kept", AUGUST),
        "media/booking_results/2026/10/05/new.pdf": (b"newbrf", after),
        "media/wallet_receipts/2026/08/receipt.pdf": (b"wallet", AUGUST),
        "media/equipment_images/2026/08/eq.jpg": (b"img", AUGUST),
    }


@pytest.mark.django_db
def test_purge_dry_run_apply_is_idempotent_scoped_and_restorable(tmp_path):
    user = UserFactory()
    booking = _booking(user)
    old_row = BookingResultFile.objects.create(booking=booking, file="booking_results/2026/08/10/oldrow.pdf")
    BookingResultFile.objects.filter(pk=old_row.pk).update(created_at=AUGUST)
    kept_row = BookingResultFile.objects.create(booking=booking, file="booking_results/2026/08/11/kept.pdf")
    fake = FakeS3(_purge_objects(booking, kept_row.file.name))
    fake.objects["media/booking_results/2026/08/10/oldrow.pdf"] = (b"oldrow", AUGUST)
    before = dict(fake.objects)

    dry = purge.run(apply=False, backup_dir=tmp_path / "bk", client=fake, bucket="b")
    assert fake.objects == before
    assert not (tmp_path / "bk").exists()
    assert dry.counts["s3:results"] == 2
    assert dry.counts["s3:booking_results"] == 2  # unreferenced orphan + file of the pre-cutoff row
    assert dry.counts["db:equipment.BookingResultFile:backup_delete"] == 1
    assert purge.reachable_old_objects(fake, "b")["bookings_seeing_older_objects"] == 1

    applied = purge.run(apply=True, backup_dir=tmp_path / "bk", client=fake, bucket="b")
    assert applied.counts["moved:results"] == 2
    assert applied.counts["moved:booking_results"] == 2
    assert not applied.errors
    vid = booking.virtual_booking_id
    assert f"Results/{vid}/old.csv" not in fake.objects
    assert f"{purge.ARCHIVE_PREFIX}Results/{vid}/old.csv" in fake.objects
    assert f"Results/{vid}/genuine.csv" in fake.objects
    assert f"media/{kept_row.file.name}" in fake.objects
    assert "media/booking_results/2026/10/05/new.pdf" in fake.objects
    assert "media/wallet_receipts/2026/08/receipt.pdf" in fake.objects
    assert "media/equipment_images/2026/08/eq.jpg" in fake.objects
    assert not BookingResultFile.objects.filter(pk=old_row.pk).exists()
    assert BookingResultFile.objects.filter(pk=kept_row.pk).exists()
    assert purge.reachable_old_objects(fake, "b")["older_objects_reachable"] == 0

    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    assert Path(applied.manifest_path).parent == tmp_path / "bk"
    assert len(manifest["moves"]) == 4
    backup_rows = json.loads(open(applied.db_backup_path, encoding="utf-8").read())
    assert [r["pk"] for r in backup_rows if r["model"] == "equipment.bookingresultfile"] == [old_row.pk]
    assert any(k.startswith(f"{purge.ARCHIVE_PREFIX}manifests/") for k in fake.objects)

    again = purge.run(apply=True, backup_dir=tmp_path / "bk2", client=fake, bucket="b")
    assert not any(k.startswith("moved:") for k in again.counts)
    assert again.counts["db:equipment.BookingResultFile:backup_delete"] == 0

    restored = purge.restore_from_manifest(applied.manifest_path, client=fake, bucket="b")
    assert restored["restored"] == 4
    assert f"Results/{vid}/old.csv" in fake.objects
    assert restored["rows_to_loaddata:equipment.BookingResultFile"] == 1


@pytest.mark.django_db
def test_purge_command_requires_confirm_for_apply(tmp_path, monkeypatch):
    from django.core.management import CommandError, call_command

    import iic_booking.sync.services.results_s3 as results_s3

    monkeypatch.setattr(results_s3, "_s3_client", lambda: (FakeS3(), "b"))
    with pytest.raises(CommandError):
        call_command("purge_prelaunch_result_data", "--apply", "--backup-dir", str(tmp_path))
    out = io.StringIO()
    call_command("purge_prelaunch_result_data", "--backup-dir", str(tmp_path), stdout=out)
    assert "mode=dry-run" in out.getvalue()
    assert not any(tmp_path.iterdir())
