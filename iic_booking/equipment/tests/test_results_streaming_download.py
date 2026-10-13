"""Large result downloads: signed browser links, streamed ZIP and single files (no in-memory buffering)."""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

from iic_booking.equipment import results_download
from iic_booking.equipment.models import Booking, BookingResultFile, BookingStatus, ChargeProfile, Equipment
from iic_booking.equipment.results_download import (
    iter_zip_stream,
    make_download_token,
    storage_file_direct_url,
    user_from_download_token,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _user():
    return UserFactory(admin_approved=True)


def _booking(user, **kwargs):
    equipment = Equipment.objects.create(
        name="Streaming Results EQ",
        code=f"SR{uuid.uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
    )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "status": BookingStatus.COMPLETED,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}202600023",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


def _result_file(booking, name, content):
    return BookingResultFile.objects.create(
        booking=booking, file=SimpleUploadedFile(name, content), original_name=name
    )


def _relative(url):
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}"


@pytest.fixture
def media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    with override_settings(AWS_STORAGE_BUCKET_NAME=""):
        yield tmp_path


@pytest.mark.django_db
def test_download_token_is_bound_to_booking_and_expires(monkeypatch):
    user = _user()
    token = make_download_token(user, 7)

    assert user_from_download_token(token, 7) == user
    assert user_from_download_token(token, 8) is None
    assert user_from_download_token("tampered" + token, 7) is None
    monkeypatch.setattr(results_download, "TOKEN_MAX_AGE_SECONDS", -1)
    assert user_from_download_token(token, 7) is None


def test_zip_stream_is_valid_and_skips_unreadable_members():
    archive = os.urandom(3 * 1024 * 1024 + 17)
    text = b"a,b\n1,2\n" * 50_000

    def broken():
        raise OSError("gone")

    chunks = list(
        iter_zip_stream(
            [
                ("IICX/13.010.2026.zip", lambda: io.BytesIO(archive)),
                ("IICX/missing.csv", broken),
                ("IICX/data.csv", lambda: io.BytesIO(text)),
            ]
        )
    )

    assert len(chunks) > 3
    assert max(len(c) for c in chunks) < 3 * 1024 * 1024
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["IICX/13.010.2026.zip", "IICX/data.csv"]
        assert zf.getinfo("IICX/13.010.2026.zip").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("IICX/data.csv").compress_type == zipfile.ZIP_DEFLATED
        assert zf.read("IICX/13.010.2026.zip") == archive
        assert zf.read("IICX/data.csv") == text


@pytest.mark.django_db
def test_zip_link_downloads_without_auth_header(media):
    user = _user()
    booking = _booking(user)
    _result_file(booking, "13.010.2026.zip", b"PK-archive")
    _result_file(booking, "notes.txt", b"hello")

    client = APIClient()
    client.force_authenticate(user=user)
    link = client.post(f"/api/bookings/{booking.pk}/results/download-link/")
    assert link.status_code == 200
    body = link.json()
    assert body["filename"] == f"Results_{booking.virtual_booking_id}.zip"
    assert "t" in parse_qs(urlparse(body["url"]).query)

    browser = APIClient()
    zipped = browser.get(_relative(body["url"]), HTTP_ACCEPT_ENCODING="gzip, deflate, br")
    assert zipped.status_code == 200
    assert zipped["Content-Type"] == "application/zip"
    assert not zipped.has_header("Content-Encoding")
    with zipfile.ZipFile(io.BytesIO(b"".join(zipped))) as zf:
        names = {name.rsplit("/", 1)[-1]: zf.read(name) for name in zf.namelist()}
    assert names == {"13.010.2026.zip": b"PK-archive", "notes.txt": b"hello"}


@pytest.mark.django_db
def test_zip_download_rejects_missing_or_foreign_token(media):
    user = _user()
    booking = _booking(user)
    other_booking = _booking(user)
    _result_file(booking, "a.txt", b"a")

    browser = APIClient()
    assert browser.get(f"/api/bookings/{booking.pk}/results/download/").status_code == 401
    foreign = make_download_token(user, other_booking.pk)
    assert browser.get(f"/api/bookings/{booking.pk}/results/download/?t={foreign}").status_code == 401

    stranger_token = make_download_token(_user(), booking.pk)
    assert browser.get(f"/api/bookings/{booking.pk}/results/download/?t={stranger_token}").status_code == 403


@pytest.mark.django_db
def test_zip_link_respects_results_gates(media):
    user = _user()
    booking = _booking(user, status=BookingStatus.BOOKED)
    _result_file(booking, "a.txt", b"a")
    empty = _booking(user)

    client = APIClient()
    client.force_authenticate(user=user)
    assert client.post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 400
    assert client.post(f"/api/bookings/{empty.pk}/results/download-link/").status_code == 404


@pytest.mark.django_db
def test_single_file_link_streams_with_length(media):
    user = _user()
    booking = _booking(user)
    payload = os.urandom(2 * 1024 * 1024 + 5)
    brf = _result_file(booking, "13.010.2026.zip", payload)

    client = APIClient()
    client.force_authenticate(user=user)
    link = client.get(f"/api/bookings/{booking.pk}/results/files/{brf.pk}/?link=1")
    assert link.status_code == 200
    assert link.json()["filename"] == "13.010.2026.zip"

    downloaded = APIClient().get(_relative(link.json()["url"]), HTTP_ACCEPT_ENCODING="gzip")
    assert downloaded.status_code == 200
    assert downloaded["Content-Length"] == str(len(payload))
    assert "13.010.2026.zip" in downloaded["Content-Disposition"]
    assert not downloaded.has_header("Content-Encoding")
    assert b"".join(downloaded) == payload


@pytest.mark.django_db
def test_zip_streams_through_async_iterator_under_asgi(media):
    from asgiref.sync import async_to_sync
    from django.test import AsyncClient

    user = _user()
    booking = _booking(user)
    payload = os.urandom(3 * 1024 * 1024)
    _result_file(booking, "13.010.2026.zip", payload)
    token = make_download_token(user, booking.pk)

    async def fetch():
        response = await AsyncClient().get(
            f"/api/bookings/{booking.pk}/results/download/?t={token}", HTTP_ACCEPT_ENCODING="gzip"
        )
        assert response.is_async
        chunks = [chunk async for chunk in response.streaming_content]
        return response, chunks

    response, chunks = async_to_sync(fetch)()
    assert response.status_code == 200
    assert response["X-Results-Total-Bytes"] == str(len(payload))
    assert not response.has_header("Content-Encoding")
    assert len(chunks) > 2
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as zf:
        assert zf.read(zf.namelist()[0]) == payload


def test_s3_direct_url_asks_storage_to_download_under_original_name():
    calls = {}

    class FakeS3Storage:
        bucket_name = "bucket"

        def url(self, name, parameters=None, expire=None):
            calls.update(name=name, parameters=parameters, expire=expire)
            return "https://s3.example/signed"

    class FakeFile:
        name = "booking_results/2026/10/13/13.010.2026.zip"
        storage = FakeS3Storage()

    assert storage_file_direct_url(FakeFile(), "13.010.2026.zip") == "https://s3.example/signed"
    assert calls["name"] == FakeFile.name
    assert calls["expire"] == results_download.LINK_EXPIRES_SECONDS
    assert calls["parameters"]["ResponseContentDisposition"].startswith("attachment;")
    assert "13.010.2026.zip" in calls["parameters"]["ResponseContentDisposition"]


def _sources(*items, in_storage=True):
    from iic_booking.equipment.results_download import ResultSource

    return [
        ResultSource(
            arcname=f"IICX/uploaded/{name}",
            size=len(data),
            ident=f"file:{name}",
            open=lambda data=data: io.BytesIO(data),
            direct_url=(lambda filename, name=name: f"https://s3.example/{name}?as={filename}") if in_storage else None,
            in_object_storage=in_storage,
        )
        for name, data in items
    ]


def _link(user, booking, **data):
    client = APIClient()
    client.force_authenticate(user=user)
    return client.post(f"/api/bookings/{booking.pk}/results/download-link/", data, format="json")


@pytest.mark.django_db
def test_single_stored_file_downloads_directly_from_storage(media, monkeypatch):
    from iic_booking.equipment import api_views

    user = _user()
    booking = _booking(user)
    monkeypatch.setattr(api_views, "_booking_result_sources", lambda b: _sources(("13.010.2026.zip", b"x" * 10)))

    body = _link(user, booking).json()

    assert body["mode"] == "direct" and body["status"] == "ready"
    assert body["url"] == "https://s3.example/13.010.2026.zip?as=13.010.2026.zip"
    assert body["filename"] == "13.010.2026.zip" and body["total_bytes"] == 10


@pytest.mark.django_db
def test_large_zip_is_built_once_then_downloaded_from_storage(media, monkeypatch):
    from iic_booking.equipment import api_views, tasks

    user = _user()
    booking = _booking(user)
    monkeypatch.setattr(results_download, "ZIP_CACHE_MIN_BYTES", 10)
    monkeypatch.setattr(api_views, "_booking_result_sources", lambda b: _sources(("a.bin", b"a" * 8), ("b.bin", b"b" * 8)))
    queued = []
    monkeypatch.setattr(tasks.build_results_zip, "delay", lambda *args: queued.append(args))
    built = {}
    monkeypatch.setattr(results_download, "cached_zip_url", lambda bid, fp, name: built.get((bid, fp)))

    first = _link(user, booking).json()
    second = _link(user, booking).json()

    assert first["status"] == second["status"] == "preparing"
    assert len(queued) == 1
    booking_id, fingerprint = queued[0]
    built[(booking_id, fingerprint)] = "https://s3.example/built.zip"
    ready = _link(user, booking).json()
    assert ready == {
        "mode": "direct", "status": "ready", "url": "https://s3.example/built.zip",
        "filename": f"Results_{booking.virtual_booking_id}.zip", "total_bytes": 16,
    }

    streamed = _link(user, booking, stream=True).json()
    assert streamed["mode"] == "stream" and "/results/download/?t=" in streamed["url"]


@pytest.mark.django_db
def test_small_or_local_results_keep_the_streamed_zip(media, monkeypatch):
    from iic_booking.equipment import api_views

    user = _user()
    booking = _booking(user)
    monkeypatch.setattr(results_download, "ZIP_CACHE_MIN_BYTES", 10)
    monkeypatch.setattr(
        api_views, "_booking_result_sources",
        lambda b: _sources(("a.bin", b"a" * 8), ("b.bin", b"b" * 8), in_storage=False),
    )

    body = _link(user, booking).json()

    assert body["mode"] == "stream" and body["status"] == "ready"


@pytest.mark.django_db
def test_build_writes_a_valid_zip_and_prunes_older_copies(monkeypatch):
    from datetime import datetime, timezone as dt_timezone

    from django.core.cache import cache

    from iic_booking.sync.services import results_s3

    uploaded, deleted = {}, []

    class FakeClient:
        def upload_fileobj(self, fileobj, bucket, key, ExtraArgs, Config, Callback):  # noqa: N803
            data = b""
            while chunk := fileobj.read(5):
                data += chunk
                Callback(len(chunk))
            uploaded[key] = data

        def get_paginator(self, name):
            class Pages:
                def paginate(self, **kwargs):
                    yield {"Contents": [
                        {"Key": "results-zip-cache/7/old.zip", "LastModified": datetime.now(dt_timezone.utc)},
                        {"Key": "results-zip-cache/7/new.zip", "LastModified": datetime.now(dt_timezone.utc)},
                        {"Key": "results-zip-cache/9/kept.zip", "LastModified": datetime.now(dt_timezone.utc)},
                        {"Key": "results-zip-cache/9/ancient.zip", "LastModified": datetime(2020, 1, 1, tzinfo=dt_timezone.utc)},
                    ]}
            return Pages()

        def delete_objects(self, Bucket, Delete):  # noqa: N803
            deleted.extend(o["Key"] for o in Delete["Objects"])

    monkeypatch.setattr(results_s3, "_s3_client", lambda: (FakeClient(), "bucket"))

    class FakeBooking:
        pk = 7

    sources = _sources(("a.csv", b"1,2\n" * 100), ("b.zip", b"PK" * 50))
    key = results_download.build_zip_into_storage(FakeBooking(), sources, "new")

    assert key == "results-zip-cache/7/new.zip"
    with zipfile.ZipFile(io.BytesIO(uploaded[key])) as zf:
        assert zf.read("IICX/uploaded/a.csv") == b"1,2\n" * 100
        assert zf.read("IICX/uploaded/b.zip") == b"PK" * 50
    assert sorted(deleted) == ["results-zip-cache/7/old.zip", "results-zip-cache/9/ancient.zip"]
    assert cache.get(results_download._build_state_key(7, "new"))["state"] == "ready"


def test_strict_zip_stream_fails_on_a_missing_member():
    with pytest.raises(FileNotFoundError):
        list(iter_zip_stream([("IICX/a.bin", lambda: None)], strict=True))


def test_presigned_results_url_carries_filename(monkeypatch):
    from iic_booking.sync.services import results_s3

    seen = {}

    class FakeClient:
        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
            seen.update(Params)
            return "https://s3.example/presigned"

    monkeypatch.setattr(results_s3, "_s3_client", lambda: (FakeClient(), "bucket"))
    assert results_s3.presign_results_s3_get("Results/x.zip", filename="x.zip") == "https://s3.example/presigned"
    assert seen["ResponseContentDisposition"].startswith("attachment;")
    seen.clear()
    assert results_s3.presign_results_s3_get("Results/x.zip")
    assert "ResponseContentDisposition" not in seen
