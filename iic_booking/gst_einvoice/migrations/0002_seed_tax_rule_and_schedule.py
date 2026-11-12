"""SRIC's present SAC and rate (998397 at 18%, from SRIC's sample invoice; marked subject to SRIC's approval), and
the 15-minute schedule. The schedule does nothing while GST_EINVOICE_ENABLED is off."""

from decimal import Decimal

from django.db import migrations

TASK = "gst_einvoice.process_pending"


def forwards(apps, schema_editor):
    TaxRule = apps.get_model("gst_einvoice", "TaxRule")
    TaxRule.objects.get_or_create(key="testing-analysis", defaults={
        "label": "Testing and analysis services", "sac_code": "998397",
        "description": "Research and development services (testing / analysis)", "gst_rate": Decimal("18.00"),
        "illustrative": True, "is_default": True, "is_active": True})
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(minute="*/15", hour="*", day_of_week="*", day_of_month="*",
                                                       month_of_year="*")
    if not PeriodicTask.objects.filter(task=TASK).exists():
        PeriodicTask.objects.create(name="GST invoices (sandbox): pending requests and unanswered IRNs (every 15 min)",
                                    task=TASK, crontab=crontab, enabled=True)


def backwards(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(task=TASK).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("gst_einvoice", "0001_initial"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
