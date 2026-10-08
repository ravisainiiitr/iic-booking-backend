"""Drop the cached Equipment PI map when PI assignments or charge profiles change."""

from django.db import transaction
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ChargeProfile
from .models import EquipmentPI
from .pi_pricing import invalidate_pi_faculty_cache


@receiver(post_save, sender=EquipmentPI, dispatch_uid="pi_pricing_equipment_pi_saved")
@receiver(post_delete, sender=EquipmentPI, dispatch_uid="pi_pricing_equipment_pi_deleted")
@receiver(post_save, sender=ChargeProfile, dispatch_uid="pi_pricing_charge_profile_saved")
@receiver(post_delete, sender=ChargeProfile, dispatch_uid="pi_pricing_charge_profile_deleted")
def _invalidate_pi_faculty(sender, **kwargs):
    invalidate_pi_faculty_cache()
    # Also after commit, so a concurrent reader cannot re-cache the pre-commit value.
    transaction.on_commit(invalidate_pi_faculty_cache)
