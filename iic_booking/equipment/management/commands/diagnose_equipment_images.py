"""
Diagnose equipment image storage (DB path vs S3 object).

Usage (on production django container):
  python manage.py diagnose_equipment_images
  python manage.py diagnose_equipment_images --equipment-id 8
  python manage.py diagnose_equipment_images --sample 20
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from iic_booking.equipment.image_utils import (
    get_equipment_image_storage_path,
    open_equipment_image_bytes,
    storage_path_candidates,
    verify_file_field_in_storage,
)
from iic_booking.equipment.models import Equipment, get_equipment_image_storage


class Command(BaseCommand):
    help = "Report whether equipment image DB paths resolve in configured storage (S3)."

    def add_arguments(self, parser):
        parser.add_argument("--equipment-id", type=int, default=None)
        parser.add_argument(
            "--sample",
            type=int,
            default=50,
            help="Max rows to inspect when no --equipment-id (default 50).",
        )
        parser.add_argument(
            "--config-only",
            action="store_true",
            help="Print storage/backend/bucket config only (no DB scan).",
        )

    def handle(self, *args, **options):
        eid = options.get("equipment_id")
        sample = int(options.get("sample") or 50)
        config_only = bool(options.get("config_only"))

        storage = get_equipment_image_storage()
        bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "(unset)"
        region = getattr(settings, "AWS_S3_REGION_NAME", "") or "(unset)"
        backend = type(storage).__name__
        location = getattr(storage, "location", "") or getattr(storage, "_location", "") or ""
        keys_set = bool(getattr(settings, "AWS_ACCESS_KEY_ID", "")) and bool(
            getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        )

        self.stdout.write(
            self.style.WARNING(
                f"Storage backend={backend} bucket={bucket} region={region} "
                f"location={location!r} aws_keys_set={keys_set} "
                f"allow_local_fallback={getattr(settings, 'ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK', False)}"
            )
        )
        self.stdout.write(
            f"Expected S3 key prefix: s3://{bucket}/{location + '/' if location else ''}equipment_images/"
        )

        if config_only:
            return

        if eid:
            qs = Equipment.objects.filter(pk=eid)
        else:
            qs = (
                Equipment.objects.exclude(image="")
                .exclude(image__isnull=True)
                .order_by("-equipment_id")[:sample]
            )

        ok = 0
        missing = 0
        empty = 0

        for equipment in qs:
            path = get_equipment_image_storage_path(equipment)
            if not path:
                empty += 1
                self.stdout.write(
                    f"[{equipment.equipment_id}] {equipment.code}: (no image path in DB)"
                )
                continue

            verified = verify_file_field_in_storage(equipment.image)
            content, resolved, _ = open_equipment_image_bytes(equipment)
            opens = bool(content and resolved)
            candidates = list(storage_path_candidates(path))
            candidate_exists = []
            for key in candidates:
                try:
                    exists = bool(storage.exists(key))
                except Exception as exc:
                    exists = f"error:{exc}"
                candidate_exists.append(f"{key}=>{exists}")

            status = "OK" if (verified and opens) else "MISSING"
            if status == "OK":
                ok += 1
                style = self.style.SUCCESS
            else:
                missing += 1
                style = self.style.ERROR

            self.stdout.write(
                style(
                    f"[{equipment.equipment_id}] {equipment.code}: {status} "
                    f"db_path={path!r} resolved={resolved!r} "
                    f"candidates=[{', '.join(candidate_exists)}]"
                )
            )

        self.stdout.write(
            f"Done. ok={ok} missing={missing} empty_db={empty} "
            f"(expected S3 keys look like s3://{bucket}/{location + '/' if location else ''}equipment_images/...)"
        )

        # Probe bucket root prefixes via default_storage only when S3.
        try:
            if hasattr(default_storage, "bucket") or "S3" in backend:
                self.stdout.write(
                    "Tip: In AWS console, confirm objects under "
                    f"s3://{bucket}/{location + '/' if location else ''}equipment_images/ "
                    "immediately after upload, then again after ~10 minutes. "
                    "This deploy has no bucket lifecycle rule deleting them from app config."
                )
        except Exception:
            pass
