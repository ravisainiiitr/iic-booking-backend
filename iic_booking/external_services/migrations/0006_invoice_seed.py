"""Seed for invoices: the proposed early-invoice disclosure (NOT approved) and the invoice schedules.

Invoice requests, early invoices and SRIC dispatch all stay off by default (``ExternalServicesSettings``); the
schedules do nothing until they are switched on.
"""

from django.db import migrations

EARLY_INVOICE_PROPOSED = (
    "If you request the official GST invoice before sample analysis is completed, please note that if the analysis "
    "subsequently cannot be performed due to equipment malfunction or another applicable operational reason, the "
    "refund may be limited to the eligible base amount where GST has already been remitted to the Government. Any "
    "refund will be handled under the applicable Institute rules. Please review and acknowledge this condition "
    "before submitting an early invoice request."
)

SCHEDULES = [
    # (name, task, minute, hour, day_of_week)  Celery crontab: day_of_week 0 = Sunday, so 1-5 = Monday-Friday.
    ("External invoices: queue requests for completed bookings (every 30 min)",
     "external_services.queue_completed_invoice_requests", "*/30", "*", "*"),
    ("External invoices: SRIC batch 09:30 IST working days (retries until 17:00)",
     "external_services.sric_invoice_batch", "0,30", "9-17", "1-5"),
]


def forwards(apps, schema_editor):
    PolicyText = apps.get_model("external_services", "PolicyText")
    if not PolicyText.objects.filter(key="early_invoice").exists():
        PolicyText.objects.create(
            key="early_invoice", version=1, text=EARLY_INVOICE_PROPOSED, approved=False,
            approval_note="Proposed wording from the requirements; awaiting SRIC / Institute finance approval.",
        )
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for name, task, minute, hour, dow in SCHEDULES:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour=hour, day_of_week=dow, day_of_month="*", month_of_year="*"
        )
        if not PeriodicTask.objects.filter(task=task).exists():
            PeriodicTask.objects.create(name=name, task=task, crontab=crontab, enabled=True)


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task__in=[t for _, t, *_rest in SCHEDULES]).delete()
    PolicyText = apps.get_model("external_services", "PolicyText")
    InvoicePreference = apps.get_model("external_services", "InvoicePreference")
    seeded = PolicyText.objects.filter(key="early_invoice", version=1, approved=False)
    if not InvoicePreference.objects.filter(disclosure_policy__in=seeded).exists():
        seeded._raw_delete(seeded.db)


class Migration(migrations.Migration):
    dependencies = [
        ("external_services", "0005_invoices_delivery_receipt"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
