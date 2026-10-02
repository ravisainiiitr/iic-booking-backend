"""Drop cached peak-window settings/schedules when the inputs change."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Equipment, InternalUserSlotWindowSetting, PeakWindowSetting
from .peak_window import invalidate_peak_window_cache


@receiver(post_save, sender=PeakWindowSetting, dispatch_uid="peak_window_setting_saved")
@receiver(post_delete, sender=PeakWindowSetting, dispatch_uid="peak_window_setting_deleted")
@receiver(post_save, sender=InternalUserSlotWindowSetting, dispatch_uid="peak_window_slot_window_saved")
@receiver(post_delete, sender=InternalUserSlotWindowSetting, dispatch_uid="peak_window_slot_window_deleted")
@receiver(post_save, sender=Equipment, dispatch_uid="peak_window_equipment_saved")
@receiver(post_delete, sender=Equipment, dispatch_uid="peak_window_equipment_deleted")
def _invalidate_peak_window(sender, **kwargs):
    invalidate_peak_window_cache()
    # Also after commit, so a concurrent reader cannot re-cache the pre-commit value.
    transaction.on_commit(invalidate_peak_window_cache)
