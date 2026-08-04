"""Publish Equipment PC Configuration Wizard EXE as latest portal release."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from iic_booking.deployment.models import EquipmentPcWizardRelease


class Command(BaseCommand):
    help = "Publish EquipmentPcConfigurationWizard.exe as EquipmentPcWizardRelease."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to wizard EXE")
        parser.add_argument(
            "--release-version",
            required=True,
            dest="release_version",
            help="Release version string (e.g. 1.0.0)",
        )
        parser.add_argument("--build", default="", dest="build_number")
        parser.add_argument("--channel", default="stable")
        parser.add_argument("--notes", default="")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        version = options["release_version"]
        rel = EquipmentPcWizardRelease(
            version=version,
            build_number=options.get("build_number") or "",
            channel=options["channel"],
            release_date=date.today(),
            release_notes=options["notes"] or f"Published {version}",
            sha256=digest,
            signature_status=EquipmentPcWizardRelease.SignatureStatus.UNSIGNED,
            download_size_bytes=path.stat().st_size,
            original_name=path.name,
            is_active=True,
            installation_guide_url="/deployment-center",
            documentation_url="/deployment-center",
            troubleshooting_guide_url="/deployment-center",
        )
        with path.open("rb") as fh:
            rel.file.save(path.name, File(fh), save=False)
        rel.save()
        rel.mark_latest()
        self.stdout.write(self.style.SUCCESS(f"Published wizard {rel.version} sha256={rel.sha256}"))
