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
    """Full idempotent sync (catches temporary OIC / leave cover ending by time) and lifts ended suspensions."""
    if not _enabled():
        return None
    from .services.membership import lift_expired_suspensions
    from .services.membership import sync_all

    lifted = lift_expired_suspensions()
    report = sync_all(dry_run=False, source="nightly")
    return {"lifted_suspensions": lifted, **report.as_dict()}
