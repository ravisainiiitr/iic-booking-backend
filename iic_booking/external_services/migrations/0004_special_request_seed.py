"""Seed data for special requests: the proposed short-notice wording (NOT approved), the generic notice email template,
and the expiry and notification-sweep schedules.

The wording is the text proposed in the requirements. It is stored with ``approved=False``; IIC administration must
approve it (or replace it with a new version) before the feature is switched on. Special requests stay off by
default (``ExternalServicesSettings.special_requests_enabled``).
"""

from django.db import migrations

SHORT_NOTICE_PROPOSED = (
    "Short-notice booking conditions: If your requested slot is less than 12 days away, you must either submit the "
    "sample in person or ensure that the sample reaches the designated receiving office before the scheduled time. "
    "If the sample does not arrive in time, the slot may be treated as unused. No refund will be entertained on this "
    "basis. Allocation remains subject to Officer in Charge approval and realization of payment within the "
    "applicable deadline. Please confirm that you understand these conditions before submitting your request."
)

TEMPLATE = {
    "code": "external_service_notice",
    "name": "External services notice (special requests, invoices, dispatch)",
    "subject": "[IIC Booking] {{ title }}",
    "body_text": "Dear {{ user_name }},\n\n{{ message }}\n\nOpen in the portal: {{ link }}\n\nRegards,\nIIC Booking Team\n",
    "body_html": (
        "<p>Dear <b>{{ user_name }}</b>,</p><p>{{ message }}</p>"
        "<p><a href=\"{{ link }}\">Open in the portal</a></p><p>Regards,<br/>IIC Booking Team</p>"
    ),
    "description": "Generic message for external-user services (special requests, payment deadlines, invoices).",
    "variable_help": "user_name, title, message, link",
}

SCHEDULES = [
    ("External special requests: expire unpaid allocations (every 5 min)",
     "external_services.expire_special_allocations", "*/5"),
    ("External services: notification retry sweep (every 10 min)", "external_services.notifications_sweep", "*/10"),
]


def forwards(apps, schema_editor):
    PolicyText = apps.get_model("external_services", "PolicyText")
    if not PolicyText.objects.filter(key="short_notice").exists():
        PolicyText.objects.create(
            key="short_notice", version=1, text=SHORT_NOTICE_PROPOSED, approved=False,
            approval_note="Proposed wording from the requirements; awaiting IIC administration approval.",
        )
    Template = apps.get_model("communication", "CommunicationTemplate")
    Template.objects.update_or_create(
        code=TEMPLATE["code"],
        defaults={k: v for k, v in TEMPLATE.items() if k != "code"} | {"communication_type": "email", "is_active": True},
    )
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for name, task, minute in SCHEDULES:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour="*", day_of_week="*", day_of_month="*", month_of_year="*"
        )
        # Both tasks do nothing until there is work (special requests are off by default).
        if not PeriodicTask.objects.filter(task=task).exists():
            PeriodicTask.objects.create(name=name, task=task, crontab=crontab, enabled=True)


def backwards(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task__in=[t for _, t, _ in SCHEDULES]).delete()
    Template = apps.get_model("communication", "CommunicationTemplate")
    Template.objects.filter(code=TEMPLATE["code"]).delete()
    # Policy texts are append-only records; the seeded unapproved version is removed only if never acknowledged.
    PolicyText = apps.get_model("external_services", "PolicyText")
    SpecialRequestDetail = apps.get_model("external_services", "SpecialRequestDetail")
    SpecialAllocation = apps.get_model("external_services", "SpecialAllocation")
    seeded = PolicyText.objects.filter(key="short_notice", version=1, approved=False)
    used = SpecialRequestDetail.objects.filter(short_notice_policy__in=seeded).exists() or (
        SpecialAllocation.objects.filter(short_notice_policy__in=seeded).exists()
    )
    if not used:
        seeded._raw_delete(seeded.db)


class Migration(migrations.Migration):
    dependencies = [
        ("external_services", "0003_special_requests"),
        ("communication", "0063_maintenance_hold_update_email_template"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
