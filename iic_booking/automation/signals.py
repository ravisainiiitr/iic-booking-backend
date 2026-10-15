from django.db.models.signals import post_save
from django.dispatch import receiver

from iic_booking.support.models import Ticket


@receiver(post_save, sender=Ticket, dispatch_uid="automation_ticket_triage")
def ticket_created_triage(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from .ticket_triage import schedule_triage

        schedule_triage(instance.pk)
