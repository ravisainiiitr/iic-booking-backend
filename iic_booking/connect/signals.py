"""Keep community membership current from bookings, staff assignments and equipment groups.

* Every receiver is a no-op while IITR_CONNECT_ENABLED is off (the full sync command catches up when it is
  switched on).
* Work runs after the surrounding transaction commits and never raises, so a booking or an assignment change can
  never fail because of Connect. During the slot-opening peak window the work is handed to Celery, which defers it.
* Booking code is not modified: these are ordinary post_save / post_delete receivers.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.db.models.signals import post_init
from django.db.models.signals import post_save
from django.dispatch import receiver

from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentGroup
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.equipment.models import EquipmentOperatorCoverage
from iic_booking.equipment.models import EquipmentTemporaryOIC

logger = logging.getLogger(__name__)

_BOOKING_STATUS_ATTR = "_connect_loaded_status"
_EQUIPMENT_GROUP_ATTR = "_connect_loaded_group"


def _enabled() -> bool:
    from .access import feature_enabled

    return feature_enabled()


def _in_peak() -> bool:
    try:
        from iic_booking.equipment.peak_window import (
            seconds_until_peak_end_for_deferral,
        )

        return bool(seconds_until_peak_end_for_deferral())
    except Exception:  # noqa: BLE001
        return False


def _after_commit(fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - membership sync must never break the triggering action
            logger.warning("IITR Connect: membership sync failed for %s%r", fn.__name__, args, exc_info=True)

    transaction.on_commit(run)


def _sync_users(user_ids, group_ids) -> None:
    user_ids, group_ids = sorted(set(user_ids)), sorted(set(group_ids))
    if not user_ids or not group_ids:
        return
    if _in_peak():
        from .tasks import sync_users_in_groups_task

        sync_users_in_groups_task.delay(user_ids, group_ids)
        return
    from .services.membership import sync_users_in_groups

    sync_users_in_groups(user_ids, group_ids)


def _sync_groups(group_ids) -> None:
    group_ids = sorted({g for g in group_ids if g})
    if not group_ids:
        return
    if _in_peak():
        from .tasks import sync_groups_task

        sync_groups_task.delay(group_ids)
        return
    from .services.membership import sync_groups

    sync_groups(group_ids)


def _groups_for_equipment_id(equipment_id) -> set[int]:
    from .services.membership import group_ids_for_equipment

    return group_ids_for_equipment([equipment_id])


# --- Bookings ---------------------------------------------------------------------------------------------------


@receiver(post_init, sender=Booking)
def _remember_booking_status(sender, instance, **kwargs):
    instance.__dict__[_BOOKING_STATUS_ATTR] = instance.__dict__.get("status")


@receiver(post_save, sender=Booking)
def _booking_saved(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    status = instance.__dict__.get("status")
    previous = instance.__dict__.get(_BOOKING_STATUS_ATTR)
    instance.__dict__[_BOOKING_STATUS_ATTR] = status
    if not _enabled():
        return
    if not created and status == previous:
        return
    if status == BookingStatus.PENDING_PAYMENT or not instance.user_id or not instance.equipment_id:
        return
    user_id, equipment_id = instance.user_id, instance.equipment_id

    def run():
        _sync_users([user_id], _groups_for_equipment_id(equipment_id))

    _after_commit(run)


# --- Staff assignments ------------------------------------------------------------------------------------------


def _assignment_changed(sender, instance, raw=False, **kwargs):
    if raw or not _enabled():
        return
    equipment_id = getattr(instance, "equipment_id", None)
    if not equipment_id:
        return

    def run():
        _sync_groups(_groups_for_equipment_id(equipment_id))

    _after_commit(run)


for _model in (EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC, EquipmentOperatorCoverage):
    post_save.connect(_assignment_changed, sender=_model, dispatch_uid=f"connect_assign_save_{_model.__name__}")
    post_delete.connect(_assignment_changed, sender=_model, dispatch_uid=f"connect_assign_delete_{_model.__name__}")


# --- Equipment moved between groups -------------------------------------------------------------------------------


@receiver(post_init, sender=Equipment)
def _remember_equipment_group(sender, instance, **kwargs):
    instance.__dict__[_EQUIPMENT_GROUP_ATTR] = (
        instance.__dict__.get("equipment_group_id"),
        instance.__dict__.get("parent_equipment_id"),
        instance.__dict__.get("visible_to_test_accounts_only"),
    )


@receiver(post_save, sender=Equipment)
def _equipment_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    current = (instance.equipment_group_id, instance.parent_equipment_id, instance.visible_to_test_accounts_only)
    previous = instance.__dict__.get(_EQUIPMENT_GROUP_ATTR)
    instance.__dict__[_EQUIPMENT_GROUP_ATTR] = current
    if not _enabled() or (not created and previous == current):
        return
    old_group = previous[0] if previous else None
    equipment_id = instance.pk

    def run():
        groups = _groups_for_equipment_id(equipment_id)
        if old_group:
            groups.add(old_group)
        _sync_groups(groups)

    _after_commit(run)


# --- Equipment groups -------------------------------------------------------------------------------------------


@receiver(post_save, sender=EquipmentGroup)
def _group_saved(sender, instance, created, raw=False, **kwargs):
    if raw or not _enabled():
        return
    group_id = instance.pk

    def run():
        from .services.membership import ensure_community

        group = EquipmentGroup.objects.filter(pk=group_id).first()
        if group is not None:
            with transaction.atomic():
                ensure_community(group)

    _after_commit(run)


@receiver(post_delete, sender=EquipmentGroup)
def _group_deleted(sender, instance, **kwargs):
    if not _enabled():
        return

    def run():
        from .services.membership import archive_orphaned_communities

        with transaction.atomic():
            archive_orphaned_communities()

    _after_commit(run)


# --- Module settings cache -----------------------------------------------------------------------------------------


def _settings_saved(sender, **kwargs):
    from .services.threads import clear_settings_cache

    clear_settings_cache()


def _connect_settings_signal():
    from .models import ConnectSettings

    post_save.connect(_settings_saved, sender=ConnectSettings, dispatch_uid="connect_settings_cache_clear")


_connect_settings_signal()
