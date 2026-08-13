"""R12 test fixtures — avoid S3/network during unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _r12_local_file_storage(settings, tmp_path):
    """Use local filesystem storage so BookingResultFile does not hit S3."""
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    settings.MEDIA_ROOT = str(media)
    settings.MEDIA_URL = "/media/"
    settings.STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(media)},
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    # Clear any cached default storage singleton.
    from django.core.files.storage import storages

    try:
        storages._storages.clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
