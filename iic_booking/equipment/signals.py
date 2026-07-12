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


@receiver(post_save, sender=Equipment)
def _equipment_status_maintenance_hook(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_equipment_status", None)
    from .maintenance_policy import on_equipment_status_changed

    on_equipment_status_changed(instance, old, instance.status)
