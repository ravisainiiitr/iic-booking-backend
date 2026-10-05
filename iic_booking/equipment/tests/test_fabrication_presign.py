"""Presigned STL / DXF download links must point at the real bucket key (storage location prefix included)."""

from __future__ import annotations

from types import SimpleNamespace

from iic_booking.equipment import print_3d_views


class _LocationStorage:
    bucket_name = "equip-booking-media"
    location = "media"

    def __init__(self, existing):
        self._existing = set(existing)

    def exists(self, name):
        return name in self._existing

    def _normalize_name(self, name):
        return f"{self.location}/{name}"


def _s3_settings(settings):
    settings.STORAGES = {**settings.STORAGES, "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}}
    settings.AWS_STORAGE_BUCKET_NAME = "equip-booking-media"


def test_presigned_url_uses_location_prefixed_key(settings, monkeypatch):
    _s3_settings(settings)
    seen = {}

    def fake_presign(**kwargs):
        seen.update(kwargs)
        return "https://signed.example/x"

    monkeypatch.setattr("iic_booking.common_download._boto3_presign", fake_presign)
    name = "print_stl/2026/10/05/abc.stl"
    field = SimpleNamespace(name=name, storage=_LocationStorage({name}))

    url = print_3d_views.presigned_design_file_url(field, "Bunny.stl", "model.stl")

    assert url == "https://signed.example/x"
    assert seen["key"] == f"media/{name}"
    assert seen["bucket"] == "equip-booking-media"
    assert seen["download_name"] == "Bunny.stl"


def test_presigned_url_is_none_when_file_is_missing(settings, monkeypatch):
    _s3_settings(settings)
    monkeypatch.setattr("iic_booking.common_download._boto3_presign", lambda **kw: "https://signed.example/x")
    field = SimpleNamespace(name="laser_dxf/2026/10/05/gone.dxf", storage=_LocationStorage(set()))

    assert print_3d_views.presigned_design_file_url(field, "gone.dxf", "part.dxf") is None
