"""Housekeeping for My Research uploads."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="my_research.cleanup_stale_uploads")
def cleanup_stale_uploads(limit: int = 500) -> dict:
    """
    Resolve uploads left in PENDING_UPLOAD past the TTL (browser closed, network drop, confirm lost).

    - Object present with the expected size: finalize it (the upload succeeded, only the confirm call was lost).
    - Object missing: abort any multipart upload and mark FAILED.
    - S3 temporarily failing: leave the row untouched and retry on the next run.
    Valid research data is never deleted by this task.
    """
    from . import storage
    from .models import FileStatus, ResearchFile
    from .services import UploadVerificationError, finalize_upload, mark_failed

    close_old_connections()
    ttl = int(getattr(settings, "MY_RESEARCH_PENDING_UPLOAD_TTL_HOURS", 24) or 24)
    cutoff = timezone.now() - timedelta(hours=ttl)
    stats = {"finalized": 0, "failed": 0, "skipped": 0}
    stale = (
        ResearchFile.objects.select_related("workspace", "uploaded_by")
        .filter(status=FileStatus.PENDING_UPLOAD, created_at__lt=cutoff)
        .order_by("created_at")[:limit]
    )
    for research_file in stale:
        try:
            if research_file.multipart_upload_id:
                key, upload_id = research_file.storage_key, research_file.multipart_upload_id
                parts = sorted(storage.list_uploaded_parts(key, upload_id=upload_id), key=lambda p: p["part_number"])
                contiguous = [p["part_number"] for p in parts] == list(range(1, len(parts) + 1))
                if parts and contiguous and sum(p["size"] for p in parts) == research_file.size_bytes:
                    storage.complete_multipart_upload(key, upload_id=upload_id, parts=parts)
                else:
                    storage.abort_multipart_upload(key, upload_id=upload_id)
                    mark_failed(research_file, "Upload was not completed in time.", delete_object=False)
                    stats["failed"] += 1
                    continue
            finalize_upload(research_file, research_file.uploaded_by)
            stats["finalized"] += 1
        except storage.ObjectNotFound:
            mark_failed(research_file, "Upload was not completed in time.", delete_object=False)
            stats["failed"] += 1
        except UploadVerificationError:
            stats["failed"] += 1
        except storage.ResearchStorageError:
            logger.warning("my_research cleanup: storage unavailable for file=%s; will retry", research_file.pk)
            stats["skipped"] += 1
        except Exception:
            logger.exception("my_research cleanup failed file=%s", research_file.pk)
            stats["skipped"] += 1
    close_old_connections()
    if any(stats.values()):
        logger.info("my_research cleanup_stale_uploads: %s", stats)
    return stats


@shared_task(name="my_research.group_update_reminders")
def group_update_reminders(limit: int = 500) -> dict:
    """
    Daily Research Group reminders. Overdue status is also computed on read; this persists it and
    sends each reminder exactly once (tracked by overdue_notified_at / due_reminder_sent_at).
    Archived groups are skipped. Does nothing while the groups flag is off.
    """
    from .group_access import groups_enabled
    from .group_models import (
        GroupStatus,
        ResearchGroupActivityAssignee,
        ResearchUpdateRequest,
        UpdateRequestStatus,
    )
    from .group_services import OPEN_ACTIVITY_STATUSES, notify_activity_due, notify_update_overdue, today

    stats = {"overdue": 0, "due_soon": 0}
    if not groups_enabled():
        return stats
    close_old_connections()
    now_date = today()
    overdue = (
        ResearchUpdateRequest.objects.select_related("group", "assigned_to")
        .filter(
            status__in=(UpdateRequestStatus.PENDING, UpdateRequestStatus.OVERDUE),
            due_date__lt=now_date,
            overdue_notified_at__isnull=True,
            group__status=GroupStatus.ACTIVE,
        )
        .order_by("due_date")[:limit]
    )
    for req in overdue:
        claimed = ResearchUpdateRequest.objects.filter(
            pk=req.pk,
            overdue_notified_at__isnull=True,
            status__in=(UpdateRequestStatus.PENDING, UpdateRequestStatus.OVERDUE),
        ).update(status=UpdateRequestStatus.OVERDUE, overdue_notified_at=timezone.now())
        if claimed:
            notify_update_overdue(req)
            stats["overdue"] += 1
    due_soon = (
        ResearchGroupActivityAssignee.objects.select_related("activity", "activity__group", "user")
        .filter(
            removed_at__isnull=True,
            due_reminder_sent_at__isnull=True,
            activity__status__in=OPEN_ACTIVITY_STATUSES,
            activity__due_date__gte=now_date,
            activity__due_date__lte=now_date + timedelta(days=1),
            activity__group__status=GroupStatus.ACTIVE,
        )
        .order_by("activity__due_date")[:limit]
    )
    for assignee in due_soon:
        claimed = ResearchGroupActivityAssignee.objects.filter(
            pk=assignee.pk, due_reminder_sent_at__isnull=True
        ).update(due_reminder_sent_at=timezone.now())
        if claimed:
            notify_activity_due(assignee)
            stats["due_soon"] += 1
    close_old_connections()
    if any(stats.values()):
        logger.info("my_research group_update_reminders: %s", stats)
    return stats
