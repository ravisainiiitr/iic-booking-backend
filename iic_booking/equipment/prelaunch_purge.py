"""Reversible removal of booking result data created before booking opened (2026-09-30 21:00 IST).

Bookings were wiped before launch and display IDs restarted, so S3 objects under ``Results/<display id>/``
from August testing surfaced on new bookings that reuse those IDs.

Scope: booking result / analysis / sample files only. Wallets, transactions, charges, users, equipment,
slots, configs and communication logs are never touched.

- S3 ``Results/`` objects uploaded before the cutoff, and pre-cutoff ``media/booking_results/`` and
  ``media/sample_trace_replies/`` objects that no database row references, are MOVED (copy, verify size,
  delete) to ``archive/pre-launch-2026-09-30/<original key>``.
- Pre-cutoff rows that are still linked to a booking: ``ResearchFile`` and ``WorkspaceFile`` are soft-deleted;
  ``BookingResultFile``, ``EquipmentResult`` (with attachments and measurements) and ``BookingSampleTrace``
  (with reply attachments) have no soft-delete flag, so they are written to a JSON backup first and then
  deleted (their files are moved to the archive like the rest).

Every apply writes ``manifest-<stamp>.json`` (every S3 move and DB action) and, when rows are removed,
``db-rows-<stamp>.json`` (Django ``dumpdata`` format) to the backup directory, and uploads the manifest to
``archive/pre-launch-2026-09-30/manifests/``. Restore: ``restore_from_manifest`` moves objects back;
``manage.py loaddata db-rows-<stamp>.json`` restores removed rows.

Output is counts only: keys and file names can contain personal data.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core import serializers
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

CUTOFF = datetime(2026, 9, 30, 15, 30, tzinfo=dt_timezone.utc)
ARCHIVE_PREFIX = "archive/pre-launch-2026-09-30/"
CONFIRM_APPLY = "PURGE-PRE-LAUNCH"
CONFIRM_RESTORE = "RESTORE-PRE-LAUNCH"
RESULTS_PREFIX = "Results/"


def media_location() -> str:
    options = (getattr(settings, "STORAGES", {}) or {}).get("default", {}).get("OPTIONS", {}) or {}
    return str(options.get("location") or "media").strip("/")


def archive_key(key: str) -> str:
    return f"{ARCHIVE_PREFIX}{key}"


@dataclass
class PurgeReport:
    counts: Counter = field(default_factory=Counter)
    moves: list[dict[str, Any]] = field(default_factory=list)
    db_actions: list[dict[str, Any]] = field(default_factory=list)
    errors: Counter = field(default_factory=Counter)
    manifest_path: str = ""
    db_backup_path: str = ""


def _list(client, bucket: str, prefix: str):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj.get("Key") or ""
            if key and not key.endswith("/"):
                yield obj


def _move(client, bucket: str, src: str, size: int) -> str:
    """Copy ``src`` to the archive, verify the size, then delete ``src``. Returns the archive key."""
    dst = archive_key(src)
    client.copy({"Bucket": bucket, "Key": src}, bucket, dst)
    head = client.head_object(Bucket=bucket, Key=dst)
    if int(head.get("ContentLength") or 0) != int(size or 0):
        raise RuntimeError("archive copy size mismatch")
    client.delete_object(Bucket=bucket, Key=src)
    return dst


def _referenced_media_names() -> set[str]:
    from iic_booking.equipment.models import BookingResultFile, BookingSampleTraceReplyAttachment

    names: set[str] = set()
    for model in (BookingResultFile, BookingSampleTraceReplyAttachment):
        names.update(n for n in model.objects.values_list("file", flat=True) if n)
    return names


def _linked_db_querysets():
    """(label, queryset, mode) for pre-cutoff rows still linked to a booking. mode: soft | backup_delete."""
    from iic_booking.equipment.models import BookingResultFile, BookingSampleTrace
    from iic_booking.my_research.models import FileStatus, ResearchFile
    from iic_booking.remote_analysis.workspace_models import WorkspaceFile
    from iic_booking.sync.models import EquipmentResult

    return [
        ("equipment.BookingResultFile", BookingResultFile.objects.filter(created_at__lt=CUTOFF), "backup_delete"),
        ("sync.EquipmentResult", EquipmentResult.objects.filter(created_at__lt=CUTOFF), "backup_delete"),
        ("equipment.BookingSampleTrace", BookingSampleTrace.objects.filter(created_at__lt=CUTOFF), "backup_delete"),
        (
            "remote_analysis.WorkspaceFile",
            WorkspaceFile.objects.filter(uploaded_at__lt=CUTOFF, workspace__booking__isnull=False, deleted=False),
            "soft",
        ),
        (
            "my_research.ResearchFile",
            ResearchFile.objects.filter(created_at__lt=CUTOFF, booking__isnull=False).exclude(status=FileStatus.DELETED),
            "soft",
        ),
    ]


def _row_file_keys(label: str, rows) -> list[str]:
    """S3 keys of files owned by rows that are about to be removed."""
    loc = media_location()
    keys: list[str] = []
    if label == "equipment.BookingResultFile":
        keys += [f"{loc}/{r.file.name}" for r in rows if r.file]
    elif label == "sync.EquipmentResult":
        for r in rows:
            keys += [a.s3_key.strip() for a in r.attachments.all() if (a.s3_key or "").strip()]
    elif label == "equipment.BookingSampleTrace":
        for r in rows:
            keys += [f"{loc}/{a.file.name}" for a in r.reply_attachments.all() if a.file]
    return keys


def _backup_objects(label: str, rows) -> list:
    objs = list(rows)
    if label == "sync.EquipmentResult":
        for r in rows:
            objs += list(r.attachments.all()) + list(r.measurements.all())
    elif label == "equipment.BookingSampleTrace":
        for r in rows:
            objs += list(r.reply_attachments.all())
    return objs


def reachable_old_objects(client, bucket: str) -> dict[str, int]:
    """Bookings that would see S3 Results/ objects older than themselves through the display-ID scan."""
    from iic_booking.equipment.models import Booking

    index: dict[str, list] = {}
    for obj in _list(client, bucket, RESULTS_PREFIX):
        for seg in set(obj["Key"].split("/")[1:-1]):
            index.setdefault(seg, []).append(obj["LastModified"])
    bookings = objects = 0
    for vid, created in Booking.objects.exclude(virtual_booking_id__isnull=True).values_list(
        "virtual_booking_id", "created_at"
    ):
        older = [t for t in index.get((vid or "").strip(), []) if t < created]
        if older:
            bookings += 1
            objects += len(older)
    return {"bookings_seeing_older_objects": bookings, "older_objects_reachable": objects}


def run(*, apply: bool, backup_dir: str | Path, client=None, bucket: str | None = None) -> PurgeReport:
    """Dry run (default) or apply. Idempotent: a second apply finds nothing left to move."""
    from iic_booking.sync.services.results_s3 import _s3_client

    report = PurgeReport()
    if client is None:
        client, bucket = _s3_client()
    stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    backup = Path(backup_dir)

    # 1) DB rows still linked to a booking.
    db_keys: list[str] = []
    backup_objs: list = []
    db_plan: list[tuple[str, Any, str]] = []
    for label, qs, mode in _linked_db_querysets():
        n = qs.count()
        report.counts[f"db:{label}:{mode}"] = n
        if n:
            db_plan.append((label, qs, mode))
            if mode == "backup_delete":
                rows = list(qs)
                db_keys += _row_file_keys(label, rows)
                backup_objs += _backup_objects(label, rows)

    # 2) S3 objects.
    s3_plan: list[tuple[str, int, str, str]] = []
    if client is not None:
        for obj in _list(client, bucket, RESULTS_PREFIX):
            if obj["LastModified"] < CUTOFF:
                s3_plan.append((obj["Key"], int(obj.get("Size") or 0), obj["LastModified"].isoformat(), "results"))
        refs = _referenced_media_names()
        loc = media_location()
        db_key_set = set(db_keys)
        for sub in ("booking_results/", "sample_trace_replies/"):
            for obj in _list(client, bucket, f"{loc}/{sub}"):
                key = obj["Key"]
                if obj["LastModified"] >= CUTOFF:
                    continue
                if key[len(loc) + 1 :] in refs and key not in db_key_set:
                    continue
                s3_plan.append((key, int(obj.get("Size") or 0), obj["LastModified"].isoformat(), sub.rstrip("/")))
        planned = {k for k, *_ in s3_plan}
        for key in db_keys:
            if key not in planned:
                try:
                    head = client.head_object(Bucket=bucket, Key=key)
                except Exception:  # noqa: BLE001
                    report.counts["db_file_already_missing"] += 1
                    continue
                s3_plan.append((key, int(head.get("ContentLength") or 0), "", "db_row_file"))
                planned.add(key)
        for _key, _size, _lm, kind in s3_plan:
            report.counts[f"s3:{kind}"] += 1
        archived = {o["Key"][len(ARCHIVE_PREFIX) :] for o in _list(client, bucket, ARCHIVE_PREFIX) if not o["Key"].startswith(f"{ARCHIVE_PREFIX}manifests/")}
        report.counts["s3:archive_existing"] = len(archived)
        report.counts["s3:results_keys_also_in_archive"] = sum(
            1 for o in _list(client, bucket, RESULTS_PREFIX) if o["Key"] in archived
        )
    else:
        report.counts["s3:not_configured"] = 1

    if not apply:
        return report

    # Back up rows before anything is removed.
    backup.mkdir(parents=True, exist_ok=True)
    if backup_objs:
        path = backup / f"db-rows-{stamp}.json"
        path.write_text(serializers.serialize("json", backup_objs, indent=1), encoding="utf-8")
        report.db_backup_path = str(path)

    for key, size, last_modified, kind in s3_plan:
        try:
            dst = _move(client, bucket, key, size)
        except Exception:  # noqa: BLE001
            logger.exception("prelaunch purge: S3 move failed (kind=%s)", kind)
            report.errors[f"s3:{kind}"] += 1
            continue
        report.moves.append({"src": key, "dst": dst, "size": size, "last_modified": last_modified, "kind": kind})
        report.counts[f"moved:{kind}"] += 1

    now = timezone.now()
    with transaction.atomic():
        for label, qs, mode in db_plan:
            ids = [str(pk) for pk in qs.values_list("pk", flat=True)]
            action: dict[str, Any] = {"model": label, "action": mode, "pks": ids}
            if label == "remote_analysis.WorkspaceFile":
                qs.update(deleted=True)
            elif label == "my_research.ResearchFile":
                from iic_booking.my_research.models import FileStatus

                action["prior_status"] = {str(pk): st for pk, st in qs.values_list("pk", "status")}
                qs.update(status=FileStatus.DELETED, deleted_at=now)
            else:
                qs.delete()
            report.db_actions.append(action)
            report.counts[f"db_done:{label}:{mode}"] = len(ids)

    manifest = {
        "kind": "prelaunch-purge",
        "cutoff": CUTOFF.isoformat(),
        "archive_prefix": ARCHIVE_PREFIX,
        "created_at": now.isoformat(),
        "bucket": bucket,
        "counts": dict(report.counts),
        "errors": dict(report.errors),
        "db_backup_file": Path(report.db_backup_path).name if report.db_backup_path else "",
        "moves": report.moves,
        "db_actions": report.db_actions,
    }
    path = backup / f"manifest-{stamp}.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    report.manifest_path = str(path)
    if client is not None:
        try:
            client.put_object(
                Bucket=bucket,
                Key=f"{ARCHIVE_PREFIX}manifests/manifest-{stamp}.json",
                Body=json.dumps(manifest).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:  # noqa: BLE001
            logger.exception("prelaunch purge: manifest upload failed")
            report.errors["manifest_upload"] += 1
    return report


def restore_from_manifest(manifest_path: str | Path, *, client=None, bucket: str | None = None) -> Counter:
    """Move archived objects back to their original keys and undo soft deletes. Counts only."""
    from iic_booking.sync.services.results_s3 import _s3_client

    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if client is None:
        client, bucket = _s3_client()
    out: Counter = Counter()
    for move in data.get("moves") or []:
        src, dst = move["src"], move["dst"]
        try:
            client.head_object(Bucket=bucket, Key=src)
            out["already_present"] += 1
            continue
        except Exception:  # noqa: BLE001
            pass
        try:
            client.copy({"Bucket": bucket, "Key": dst}, bucket, src)
            client.delete_object(Bucket=bucket, Key=dst)
            out["restored"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("prelaunch purge restore: move back failed")
            out["failed"] += 1
    for action in data.get("db_actions") or []:
        if action["model"] == "remote_analysis.WorkspaceFile":
            from iic_booking.remote_analysis.workspace_models import WorkspaceFile

            out["workspace_files_undeleted"] += WorkspaceFile.objects.filter(pk__in=action["pks"]).update(deleted=False)
        elif action["model"] == "my_research.ResearchFile":
            from iic_booking.my_research.models import FileStatus, ResearchFile

            prior = action.get("prior_status") or {}
            for pk in action["pks"]:
                out["research_files_undeleted"] += ResearchFile.objects.filter(pk=pk).update(
                    status=prior.get(pk, FileStatus.AVAILABLE), deleted_at=None
                )
        else:
            out[f"rows_to_loaddata:{action['model']}"] += len(action["pks"])
    return out
