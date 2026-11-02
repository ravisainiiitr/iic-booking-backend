"""Email templates for UPS notices to the Officers in Charge, and the hourly UPS health check (Celery Beat)."""

from django.db import migrations

_FOOTER = (
    '<p style="color:#666;font-size:12px">Sent by the IIC portal UPS monitor because this UPS powers instruments you '
    "look after. The department administrator manages the UPS settings.</p>"
)

TEMPLATES = (
    {
        "code": "ups_power_alert_oic",
        "name": "UPS power alert (Officer in Charge)",
        "subject": "[IIC Booking] {{ alert_title }}",
        "body_text": (
            "Dear {{ recipient_name }},\n\n{{ alert_title }} ({{ time }}).\n{{ alert_message }}\n\n"
            "What to do: {{ action }}\n\nInstruments on this UPS: {{ equipment_names }}\n"
            "Department: {{ department_name }}\n\nUPS health: {{ link }}\n\nRegards,\nIIC Booking Team\n"
        ),
        "body_html": (
            "<p>Dear <b>{{ recipient_name }}</b>,</p><p><b>{{ alert_title }}</b> ({{ time }}).<br/>{{ alert_message }}</p>"
            "<p><b>What to do:</b> {{ action }}</p>"
            "<p><b>Instruments on this UPS:</b> {{ equipment_names }}<br/><b>Department:</b> {{ department_name }}</p>"
            "<p><a href=\"{{ link }}\">Open UPS health</a></p>" + _FOOTER
        ),
        "description": "Sent at once to the Officers in Charge of the instruments on a UPS when it goes on battery, "
                       "its battery runs low, or it reports a fault or bypass.",
        "variable_help": "recipient_name, ups_name, alert_title, alert_message, action, time, equipment_names, "
                         "department_name, link",
    },
    {
        "code": "ups_health_attention_oic",
        "name": "UPS health check: attention needed (Officer in Charge)",
        "subject": "[IIC Booking] UPS {{ ups_name }}: {{ level_label }}",
        "body_text": (
            "Dear {{ recipient_name }},\n\nThe UPS health check found something that needs your attention.\n\n"
            "{{ summary }}\n\nFindings:\n{{ findings_text }}\n\n"
            "Instruments on this UPS: {{ equipment_names }}\nDepartment: {{ department_name }}\n\n"
            "{{ summary_note }}\nUPS health: {{ link }}\n\nRegards,\nIIC Booking Team\n"
        ),
        "body_html": (
            "<p>Dear <b>{{ recipient_name }}</b>,</p>"
            "<p>The UPS health check found something that needs your attention.</p>"
            "<p style=\"padding:8px 12px;border-left:3px solid #d97706;background:#fffbeb\">{{ summary }}</p>"
            "<p><b>Findings</b></p>{{ findings_html }}"
            "<p><b>Instruments on this UPS:</b> {{ equipment_names }}<br/><b>Department:</b> {{ department_name }}</p>"
            "<p style=\"color:#666;font-size:12px\">{{ summary_note }}</p>"
            "<p><a href=\"{{ link }}\">Open UPS health</a></p>" + _FOOTER
        ),
        "description": "Sent to the Officers in Charge when the hourly UPS health check finds a new or worse problem, "
                       "or an early sign of one (repeated after 12 hours if urgent, 7 days if it needs attention).",
        "variable_help": "recipient_name, ups_name, level_label, summary, findings_html, findings_text, summary_note, "
                         "risk_score, equipment_names, department_name, link",
    },
)

TASK = ("UPS health: hourly AI check and officer notices", "power.assess_ups_health")


def forwards(apps, schema_editor):
    Template = apps.get_model("communication", "CommunicationTemplate")
    for t in TEMPLATES:
        Template.objects.update_or_create(
            code=t["code"], communication_type="email",
            defaults={k: v for k, v in t.items() if k != "code"} | {"is_active": True},
        )
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="7", hour="*", day_of_week="*", day_of_month="*", month_of_year="*")
    if not PeriodicTask.objects.filter(task=TASK[1]).exists():
        PeriodicTask.objects.create(name=TASK[0], task=TASK[1], crontab=crontab, enabled=True)


def backwards(apps, schema_editor):
    apps.get_model("communication", "CommunicationTemplate").objects.filter(
        code__in=[t["code"] for t in TEMPLATES], communication_type="email").delete()
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(task=TASK[1]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("power", "0002_ups_health"),
        ("communication", "0063_maintenance_hold_update_email_template"),
        ("django_celery_beat", "0018_improve_crontab_helptext"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
