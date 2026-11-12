"""Real-time I-STEM sync (12 Nov 2026): send each event to I-STEM within seconds instead of waiting for the 5-minute
sync, which stays as the safety net.

* A booking on an instrument whose charge profile requires an I-STEM FBR is saved (paid / booked, or marked
  Completed by the lab operator): the sync runs at once, so Create Booking + Confirm (allocate) follow the payment and
  FBR Execute follows completion.
* An external user's account becomes active (registration finished): their I-STEM user is created, or their existing
  one linked, straight away (``ISTEM_USER_CREATION_ENABLED``). IIT Roorkee internal users are still linked only when
  their first I-STEM booking is sent.

Events in quick succession share one run: the first schedules ``istem.run_sync`` a couple of seconds after the commit
and later events inside that window are picked up by it. Every remote call still goes through the outbox (dedupe
keys and job leases), so a run triggered here and the scheduled sync can never send the same request twice.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save

from . import conf

logger = logging.getLogger(__name__)

KICK_KEY = "istem:realtime-kick"
KICK_DELAY_SECONDS = 2


def realtime_enabled() -> bool:
    return conf.automatic_fbr_enabled() and not conf.missing_live_settings()


def kick(reason: str) -> bool:
    """Schedule one sync run shortly after the current transaction commits. True when a run was scheduled."""
    if not realtime_enabled():
        return False

    def _schedule():
        if not cache.add(KICK_KEY, 1, KICK_DELAY_SECONDS):
            return  # a run is already due within the window and will pick this event up
        from .tasks import run_istem_sync

        try:
            run_istem_sync.apply_async(countdown=KICK_DELAY_SECONDS)
        except Exception:  # broker down: the scheduled 5-minute sync sends it instead
            cache.delete(KICK_KEY)
            logger.warning("istem realtime: could not schedule a sync (%s)", reason, exc_info=True)

    transaction.on_commit(_schedule)
    return True


def _fbr_required(booking) -> bool:
    from iic_booking.equipment.models import ChargeProfile

    if not booking.charge_profile_id:
        return False
    return ChargeProfile.objects.filter(pk=booking.charge_profile_id, require_istem_fbr=True).exists()


def on_booking_saved(sender, instance, created, **kwargs):
    from .eligibility import ELIGIBLE_STATUSES

    if kwargs.get("raw") or instance.status not in ELIGIBLE_STATUSES:
        return
    try:
        if realtime_enabled() and _fbr_required(instance):
            kick(f"booking {instance.pk} {instance.status}")
    except Exception:  # never let I-STEM break saving a booking
        logger.warning("istem realtime: booking %s not scheduled", instance.pk, exc_info=True)


def on_user_saved(sender, instance, created, **kwargs):
    from iic_booking.users.models.user_type import UserType

    if kwargs.get("raw") or not instance.is_active:
        return
    if instance.user_type not in UserType.get_external_user_codes():
        return
    try:
        if not (realtime_enabled() and conf.user_creation_enabled()):
            return
        from .models import IstemUserLink

        if IstemUserLink.objects.filter(user=instance).exists():
            return  # already linked, waiting or failed: the booking flow and the scheduled sync take it from there
        from .outbox import enqueue_user_link

        enqueue_user_link(instance, trigger="registered")
        kick(f"user {instance.pk} registered")
    except Exception:  # never let I-STEM break registration
        logger.warning("istem realtime: user %s not queued", instance.pk, exc_info=True)


def connect() -> None:
    from django.contrib.auth import get_user_model

    from iic_booking.equipment.models import Booking

    post_save.connect(on_booking_saved, sender=Booking, dispatch_uid="istem_realtime_booking")
    post_save.connect(on_user_saved, sender=get_user_model(), dispatch_uid="istem_realtime_user")
