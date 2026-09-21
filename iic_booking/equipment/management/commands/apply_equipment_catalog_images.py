"""Apply pre-processed equipment catalog images (JPEG) onto Equipment.image / S3."""

from __future__ import annotations

from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment.image_utils import persist_equipment_image_upload
from iic_booking.equipment.models import Equipment


class Command(BaseCommand):
    help = (
        "Replace catalog images for listed equipment IDs using JPEG files named "
        "{equipment_id}.jpg in --dir (ops batch for studio-background refresh)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            type=str,
            required=True,
            help="Directory containing {equipment_id}.jpg files",
        )
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help="Must be APPLY to write changes",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List matches only; do not upload",
        )

    def handle(self, *args, **options):
        root = Path(options["dir"]).expanduser().resolve()
        if not root.is_dir():
            raise CommandError(f"Not a directory: {root}")

        files = sorted(root.glob("*.jpg")) + sorted(root.glob("*.jpeg")) + sorted(root.glob("*.png"))
        if not files:
            raise CommandError(f"No image files in {root}")

        pairs = []
        for path in files:
            stem = path.stem
            if not stem.isdigit():
                self.stdout.write(self.style.WARNING(f"Skip non-id filename: {path.name}"))
                continue
            pairs.append((int(stem), path))

        if not pairs:
            raise CommandError("No {id}.jpg files found")

        confirm = (options.get("confirm") or "").strip()
        dry = bool(options.get("dry_run"))
        if not dry and confirm != "APPLY":
            raise CommandError("Refusing to write without --confirm APPLY (or pass --dry-run)")

        ok = 0
        for eid, path in pairs:
            try:
                eq = Equipment.objects.get(pk=eid)
            except Equipment.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Missing equipment id={eid}"))
                continue
            self.stdout.write(f"{'DRY ' if dry else ''}id={eid} code={eq.code} <- {path.name} ({path.stat().st_size} bytes)")
            if dry:
                ok += 1
                continue
            content = path.read_bytes()
            if not content:
                self.stdout.write(self.style.ERROR(f"Empty file {path}"))
                continue
            upload = ContentFile(content, name=path.name)
            saved = persist_equipment_image_upload(eq, upload)
            self.stdout.write(self.style.SUCCESS(f"  saved -> {saved}"))
            ok += 1

        self.stdout.write(self.style.SUCCESS(f"Done: {ok}/{len(pairs)}"))