"""Publish a built DepartmentSyncAgentSetup.exe as the latest portal release."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from iic_booking.sync.installer.models import DsaInstallerRelease


class Command(BaseCommand):
    help = "Publish a DSA installer EXE as DsaInstallerRelease (marks latest)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=str, help="Path to DepartmentSyncAgentSetup.exe")
        parser.add_argument(
            "--release-version",
            required=True,
            dest="release_version",
            help="Installer release version string (e.g. 1.0.0)",
        )
        parser.add_argument("--channel", default="stable")
        parser.add_argument("--notes", default="")
        parser.add_argument("--offline", default="", help="Optional offline package path")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        version = options["release_version"]
        rel = DsaInstallerRelease(
            version=version,
            channel=options["channel"],
            release_date=date.today(),
            release_notes=options["notes"] or f"Published {version}",
            sha256=digest,
            signature_status=DsaInstallerRelease.SignatureStatus.UNSIGNED,
            download_size_bytes=path.stat().st_size,
            original_name=path.name,
            is_active=True,
            installation_guide_url="/department-sync/agent-installer",
            documentation_url="/department-sync/agent-installer",
            troubleshooting_guide_url="/department-sync/agent-installer",
        )
        with path.open("rb") as fh:
            rel.file.save(path.name, File(fh), save=False)
        offline = options.get("offline") or ""
        if offline:
            op = Path(offline)
            if not op.is_file():
                raise CommandError(f"Offline file not found: {op}")
            rel.offline_original_name = op.name
            with op.open("rb") as fh:
                rel.offline_file.save(op.name, File(fh), save=False)
        rel.save()
        rel.mark_latest()
        self.stdout.write(self.style.SUCCESS(f"Published {rel.version} sha256={rel.sha256}"))
