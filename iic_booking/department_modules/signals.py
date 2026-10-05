import logging

from django.db import DatabaseError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from iic_booking.users.models import Department

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Department, dispatch_uid="department_modules_new_department_off")
def start_new_department_off(sender, instance, created, raw=False, **kwargs):
    """Departments created after the switches were installed start with every module off."""
    if not created or raw:
        return
    from .services import start_new_department_off as create_off_rows

    try:
        with transaction.atomic():
            create_off_rows(instance)
    except DatabaseError:
        # The department is still treated as off without rows (access.default_cell).
        logger.exception("could not create the default module rows for new department %s", instance.pk)
