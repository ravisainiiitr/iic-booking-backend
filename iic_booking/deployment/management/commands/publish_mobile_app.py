"""Publish a signed IIC Booking Android APK as the latest mobile app release."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from iic_booking.deployment.models import MobileAppRelease

VERSION_NAME_RE = re.compile(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9.]+)?$")


class Command(BaseCommand):
    help = "Publish an IIC Booking APK (stored in default media storage) and mark it latest."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to the signed release APK")
        parser.add_argument("--version-name", required=True, dest="version_name")
        parser.add_argument("--version-code", required=True, type=int, dest="version_code")
        parser.add_argument("--notes", default="")
        parser.add_argument("--signing-cert-sha256", default="", dest="signing_cert_sha256")
        parser.add_argument("--expect-sha256", default="", dest="expect_sha256")
        parser.add_argument("--no-latest", action="store_true", dest="no_latest")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        if path.suffix.lower() != ".apk":
            raise CommandError("Only .apk files can be published.")
        version_name = options["version_name"].strip()
        version_code = int(options["version_code"])
        if not VERSION_NAME_RE.match(version_name):
            raise CommandError("version-name must look like 1.0.0")
        if version_code < 1:
            raise CommandError("version-code must be a positive integer")
        raw = path.read_bytes()
        if raw[:2] != b"PK":
            raise CommandError("The file is not a valid APK (zip) archive.")
        digest = hashlib.sha256(raw).hexdigest()
        expected = (options.get("expect_sha256") or "").strip().lower()
        if expected and expected != digest:
            raise CommandError(f"SHA-256 mismatch: expected {expected}, got {digest}")
        existing = MobileAppRelease.objects.filter(
            platform=MobileAppRelease.Platform.ANDROID, version_code=version_code, is_active=True
        ).first()
        if existing and existing.sha256 == digest:
            if not options["no_latest"]:
                existing.mark_latest()
            self.stdout.write(self.style.SUCCESS(f"Already published {existing.version_name} ({version_code})"))
            return
        if existing:
            raise CommandError(f"versionCode {version_code} is already published with a different file.")

        file_name = f"IIC-Booking-{version_name}.apk"
        rel = MobileAppRelease(
            platform=MobileAppRelease.Platform.ANDROID,
            version_name=version_name,
            version_code=version_code,
            release_date=date.today(),
            release_notes=options["notes"] or f"IIC Booking {version_name}",
            sha256=digest,
            signing_cert_sha256=(options.get("signing_cert_sha256") or "").strip().upper(),
            download_size_bytes=len(raw),
            original_name=file_name,
            is_active=True,
        )
        with path.open("rb") as fh:
            rel.file.save(file_name, File(fh), save=False)
        rel.save()
        if not options["no_latest"]:
            rel.mark_latest()
        self.stdout.write(
            self.style.SUCCESS(
                f"Published Android app {rel.version_name} ({rel.version_code}) sha256={rel.sha256} size={rel.download_size_bytes}"
            )
        )
