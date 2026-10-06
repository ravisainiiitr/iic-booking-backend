"""Side effects when equipment or related models change."""

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Equipment


@receiver(pre_save, sender=Equipment)
def _equipment_cache_old_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._old_equipment_status = None
        return
    try:
        instance._old_equipment_status = (
            Equipment.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )
    except Exception:
        instance._old_equipment_status = None


@receiver(pre_save, sender=Equipment)
def _protect_equipment_image_from_accidental_clear(sender, instance, **kwargs):
    """
    Keep the existing image unless admin/API explicitly clears or replaces it.

    Accidental clears previously made images "disappear" after unrelated equipment edits.
    Set instance._allow_clear_equipment_image = True to allow intentional removal.
    """
    if not instance.pk:
        return
    if getattr(instance, "_allow_clear_equipment_image", False):
        return
    new_name = ""
    if getattr(instance, "image", None) and getattr(instance.image, "name", None):
        new_name = (instance.image.name or "").strip()
    if new_name:
        return
    try:
        prev = (
            Equipment.objects.filter(pk=instance.pk)
            .values_list("image", flat=True)
            .first()
        )
    except Exception:
        return
    if prev:
        instance.image.name = prev


@receiver(post_save, sender=Equipment)
def _equipment_status_maintenance_hook(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_equipment_status", None)
    from .maintenance_policy import on_equipment_status_changed

    on_equipment_status_changed(instance, old, instance.status)


@receiver(post_save, sender=Equipment)
def _equipment_operational_closes_status_notices(sender, instance, created, **kwargs):
    """Back to Operational from any path (status menu, equipment settings, Django admin): close its
    automatic "Under Maintenance" notices. The count is kept on the instance for the API response."""
    from .models import EquipmentStatus

    old = (getattr(instance, "_old_equipment_status", None) or "").strip()
    new = (instance.status or "").strip()
    if created or new != EquipmentStatus.ACTIVE or old == EquipmentStatus.ACTIVE:
        return
    import logging

    from iic_booking.communication.notice_board_service import expire_equipment_linked_notices
    from iic_booking.communication.utils import get_current_user

    try:
        instance._status_notices_closed = expire_equipment_linked_notices(
            equipment=instance, actor=get_current_user()
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Closing status notices failed for equipment %s", instance.pk
        )
