"""AI.9: Results S3 helper failure handling (no fabricated AWS credentials)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from iic_booking.sync.services.results_s3 import (
    build_results_s3_key,
    upload_local_file_to_results_s3,
)


def test_build_results_s3_key_shape():
    assert build_results_s3_key("VID-123", "out.csv") == "Results/VID-123/out.csv"


def test_upload_skipped_when_bucket_missing(settings, tmp_path: Path):
    settings.AWS_STORAGE_BUCKET_NAME = ""
    local = tmp_path / "sample.bin"
    local.write_bytes(b"abc")
    assert upload_local_file_to_results_s3(
        virtual_booking_id="VID-1",
        local_path=local,
        file_name="sample.bin",
    ) is None


def test_upload_returns_none_on_client_failure(settings, tmp_path: Path):
    settings.AWS_STORAGE_BUCKET_NAME = "test-bucket-not-real"
    settings.AWS_S3_REGION_NAME = "ap-south-1"
    settings.AWS_ACCESS_KEY_ID = "testing"
    settings.AWS_SECRET_ACCESS_KEY = "testing"
    local = tmp_path / "sample.bin"
    local.write_bytes(b"abc")

    mock_client = MagicMock()
    mock_client.upload_file.side_effect = RuntimeError("simulated S3 failure")

    with patch("iic_booking.sync.services.results_s3._s3_client", return_value=(mock_client, "test-bucket-not-real")):
        key = upload_local_file_to_results_s3(
            virtual_booking_id="VID-FAIL",
            local_path=local,
            file_name="sample.bin",
        )
    assert key is None
    # Failure must not invent a successful object key / metadata.
    mock_client.head_object.assert_not_called()


def test_upload_missing_local_file_returns_none(settings, tmp_path: Path):
    settings.AWS_STORAGE_BUCKET_NAME = "test-bucket-not-real"
    missing = tmp_path / "missing.bin"
    with patch(
        "iic_booking.sync.services.results_s3._s3_client",
        return_value=(MagicMock(), "test-bucket-not-real"),
    ):
        assert (
            upload_local_file_to_results_s3(
                virtual_booking_id="VID-2",
                local_path=missing,
                file_name="missing.bin",
            )
            is None
        )
