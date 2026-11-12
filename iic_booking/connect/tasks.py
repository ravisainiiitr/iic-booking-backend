"""Celery tasks for IITR Connect. Every task returns immediately while IITR_CONNECT_ENABLED is off."""

from __future__ import annotations

import logging

from celery import shared_task

from iic_booking.equipment.peak_window import defer_during_peak

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    from .access import feature_enabled

    return feature_enabled()


@shared_task(name="connect.sync_users_in_groups")
@defer_during_peak("connect.sync_users_in_groups")
def sync_users_in_groups_task(user_ids, group_ids):
    if not _enabled():
        return None
    from .services.membership import sync_users_in_groups

    return sync_users_in_groups(user_ids, group_ids, source="task").as_dict()


@shared_task(name="connect.sync_groups")
@defer_during_peak("connect.sync_groups")
def sync_groups_task(group_ids):
    if not _enabled():
        return None
    from .services.membership import sync_groups

    return sync_groups(group_ids, source="task").as_dict()


@shared_task(name="connect.nightly_membership_sync")
@defer_during_peak("connect.nightly_membership_sync")
def nightly_membership_sync():
    """Full idempotent sync (catches temporary OIC / leave cover ending by time), lifts ended suspensions and
    removes uploads that were never attached to a post within a day."""
    if not _enabled():
        return None
    from .services.attachments import purge_unbound
    from .services.membership import lift_expired_suspensions
    from .services.membership import sync_all

    lifted = lift_expired_suspensions()
    report = sync_all(dry_run=False, source="nightly")
    purged = purge_unbound(older_than_hours=24)
    from .services.recognition import recompute

    badges = recompute()
    return {"lifted_suspensions": lifted, "purged_unbound_uploads": purged, "recognition": badges, **report.as_dict()}


@shared_task(name="connect.announcement_batch")
@defer_during_peak("connect.announcement_batch")
def announcement_batch_task(announcement_id, user_ids):
    if not _enabled():
        return None
    from .services.notifications import deliver_announcement_batch

    return deliver_announcement_batch(announcement_id, user_ids)


@shared_task(name="connect.daily_digest")
@defer_during_peak("connect.daily_digest")
def daily_digest_task():
    if not _enabled():
        return None
    from .models import EmailMode
    from .services.notifications import send_digests

    return send_digests(EmailMode.DAILY)


@shared_task(name="connect.weekly_digest")
@defer_during_peak("connect.weekly_digest")
def weekly_digest_task():
    if not _enabled():
        return None
    from .models import EmailMode
    from .services.notifications import send_digests

    return send_digests(EmailMode.WEEKLY)
