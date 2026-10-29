"""Email templates and Celery Beat schedules for courier tracking.

Schedules (DatabaseScheduler, project timezone as for the other crontab tasks):
* scheduled checks 09:15 and 15:15 daily (the quota manager spreads the monthly budget; first lookups go first);
* retry sweep for notifications / webhook events every 10 minutes;
* reminders 10:30 daily;
* courier list refresh 03:30 daily (``GET /v1/couriers`` is not counted against the quota).
"""

from django.db import migrations

TEMPLATES = [
    {
        "code": "courier_shipment_registered_user",
        "name": "Courier shipment registered (user)",
        "subject": "[IIC Booking] Courier shipment registered - Booking {{ booking_id }}",
        "body_text": (
            "Dear {{ user_name }},\n\n"
            "Your courier shipment for Booking {{ booking_id }} ({{ equipment_name }}) has been registered.\n\n"
            "Courier: {{ courier_name }}\nTracking number: {{ tracking_number }}\n\n"
            "We will follow the shipment and email you when its status changes. {{ delay_note }}\n"
            "A delivery reported by the courier is not yet confirmation that the lab has your sample; lab staff will "
            "update the sample status once they receive it.\n\n"
            "View the shipment: {{ user_link }}\n\nRegards,\nIIC Booking Team\n"
        ),
        "body_html": (
            "<p>Dear <b>{{ user_name }}</b>,</p>"
            "<p>Your courier shipment for Booking <b>{{ booking_id }}</b> ({{ equipment_name }}) has been registered.</p>"
            "<p><b>Courier:</b> {{ courier_name }}<br/><b>Tracking number:</b> {{ tracking_number }}</p>"
            "<p>We will follow the shipment and email you when its status changes. {{ delay_note }}</p>"
            "<p>A delivery reported by the courier is not yet confirmation that the lab has your sample; lab staff "
            "will update the sample status once they receive it.</p>"
            "<p><a href=\"{{ user_link }}\">View the shipment</a></p><p>Regards,<br/>IIC Booking Team</p>"
        ),
        "description": "Sent to an external user who registered a courier shipment and asked for status emails.",
        "variable_help": "user_name, booking_id, equipment_name, courier_name, tracking_number, delay_note, user_link",
    },
    {
        "code": "courier_status_update_user",
        "name": "Courier shipment status update (user)",
        "subject": "[IIC Booking] Shipment update: {{ status_label }} - Booking {{ booking_id }}",
        "body_text": (
            "Dear {{ user_name }},\n\n{{ message }}\n\n"
            "Booking: {{ booking_id }} ({{ equipment_name }})\nCourier: {{ courier_name }}\n"
            "Tracking number: {{ tracking_number }}\nCourier status: {{ provider_status }}\n"
            "Latest event: {{ event_time }} {{ event_location }}\n\n"
            "{{ delay_note }}\n\nView the shipment: {{ user_link }}\n\nRegards,\nIIC Booking Team\n"
        ),
        "body_html": (
            "<p>Dear <b>{{ user_name }}</b>,</p><p>{{ message }}</p>"
            "<p><b>Booking:</b> {{ booking_id }} ({{ equipment_name }})<br/><b>Courier:</b> {{ courier_name }}<br/>"
            "<b>Tracking number:</b> {{ tracking_number }}<br/><b>Courier status:</b> {{ provider_status }}<br/>"
            "<b>Latest event:</b> {{ event_time }} {{ event_location }}</p>"
            "<p>{{ delay_note }}</p><p><a href=\"{{ user_link }}\">View the shipment</a></p>"
            "<p>Regards,<br/>IIC Booking Team</p>"
        ),
        "description": "Sent to an external user who opted in, once per meaningful courier status.",
        "variable_help": (
            "user_name, message, booking_id, equipment_name, courier_name, tracking_number, status_label, "
            "provider_status, event_time, event_location, delay_note, user_link"
        ),
    },
    {
        "code": "courier_delivery_reported_staff",
        "name": "External shipment reported delivered (OIC / lab / accounts)",
        "subject": "[IIC Booking] External shipment reported delivered - Booking {{ booking_id }}",
        "body_text": (
            "The courier reports an external sample shipment as delivered.\n\n"
            "Booking: {{ booking_id }}\nEquipment: {{ equipment_name }}\nDepartment: {{ department_name }}\n"
            "Submitted by: {{ submitted_by }} ({{ submitted_by_email }})\nSample identifiers: {{ sample_identifiers }}\n"
            "Courier: {{ courier_name }}\nTracking number: {{ tracking_number }}\n"
            "Reported status: {{ provider_status }} ({{ event_time }} {{ event_location }})\n\n"
            "Action required: verify physical receipt of the sample and update the sample status in the portal. "
            "The sample has NOT been marked as received.\n\n"
            "Open the booking: {{ staff_link }}\n"
        ),
        "body_html": (
            "<p>The courier reports an external sample shipment as <b>delivered</b>.</p>"
            "<p><b>Booking:</b> {{ booking_id }}<br/><b>Equipment:</b> {{ equipment_name }}<br/>"
            "<b>Department:</b> {{ department_name }}<br/><b>Submitted by:</b> {{ submitted_by }} "
            "({{ submitted_by_email }})<br/><b>Sample identifiers:</b> {{ sample_identifiers }}<br/>"
            "<b>Courier:</b> {{ courier_name }}<br/><b>Tracking number:</b> {{ tracking_number }}<br/>"
            "<b>Reported status:</b> {{ provider_status }} ({{ event_time }} {{ event_location }})</p>"
            "<p><b>Action required:</b> verify physical receipt of the sample and update the sample status in the "
            "portal. The sample has <b>not</b> been marked as received.</p>"
            "<p><a href=\"{{ staff_link }}\">Open the booking</a></p>"
        ),
        "description": "Sent to the equipment's OIC, Lab Operators and the department's Accounts In Charge.",
        "variable_help": (
            "booking_id, equipment_name, department_name, submitted_by, submitted_by_email, sample_identifiers, "
            "courier_name, tracking_number, provider_status, event_time, event_location, staff_link"
        ),
    },
    {
        "code": "courier_reminder_user",
        "name": "Sample arrival reminder (user)",
        "subject": "[IIC Booking] Sample arrival - Booking {{ booking_id }}",
        "body_text": (
            "Dear {{ user_name }},\n\n{{ message }}\n\nBooking: {{ booking_id }} ({{ equipment_name }})\n"
            "{% if courier_name %}Courier: {{ courier_name }}\nTracking number: {{ tracking_number }}\n{% endif %}\n"
            "Update the details: {{ user_link }}\n\nRegards,\nIIC Booking Team\n"
        ),
        "body_html": (
            "<p>Dear <b>{{ user_name }}</b>,</p><p>{{ message }}</p>"
            "<p><b>Booking:</b> {{ booking_id }} ({{ equipment_name }})"
            "{% if courier_name %}<br/><b>Courier:</b> {{ courier_name }}<br/><b>Tracking number:</b> "
            "{{ tracking_number }}{% endif %}</p>"
            "<p><a href=\"{{ user_link }}\">Update the details</a></p><p>Regards,<br/>IIC Booking Team</p>"
        ),
        "description": "Reminders to external users about choosing an arrival method or a shipment needing attention.",
        "variable_help": "user_name, message, booking_id, equipment_name, courier_name, tracking_number, user_link",
    },
]

CRONTABS = [
    ("Courier tracking scheduled checks", "courier_tracking.scheduled_checks", {"minute": "15", "hour": "9,15"}),
    ("Courier tracking reminders", "courier_tracking.reminders", {"minute": "30", "hour": "10"}),
    ("Courier tracking courier list refresh", "courier_tracking.refresh_catalog", {"minute": "30", "hour": "3"}),
]
SWEEP = ("Courier tracking retry sweep", "courier_tracking.sweep", 10)


def forwards(apps, schema_editor):
    Template = apps.get_model("communication", "CommunicationTemplate")
    for t in TEMPLATES:
        Template.objects.update_or_create(
            code=t["code"],
            communication_type="email",
            defaults={k: v for k, v in t.items() if k != "code"} | {"is_active": True},
        )
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for name, task, cron in CRONTABS:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=cron["minute"], hour=cron["hour"], day_of_week="*", day_of_month="*", month_of_year="*"
        )
        if not PeriodicTask.objects.filter(task=task).exists():
            PeriodicTask.objects.create(name=name, task=task, crontab=crontab, enabled=True)
    name, task, minutes = SWEEP
    interval, _ = IntervalSchedule.objects.get_or_create(every=minutes, period="minutes")
    if not PeriodicTask.objects.filter(task=task).exists():
        PeriodicTask.objects.create(name=name, task=task, interval=interval, enabled=True)


def backwards(apps, schema_editor):
    Template = apps.get_model("communication", "CommunicationTemplate")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    Template.objects.filter(code__in=[t["code"] for t in TEMPLATES], communication_type="email").delete()
    PeriodicTask.objects.filter(task__in=[t for _, t, _ in CRONTABS] + [SWEEP[1]]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("courier_tracking", "0001_initial"),
        ("communication", "0060_monthly_reports"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
