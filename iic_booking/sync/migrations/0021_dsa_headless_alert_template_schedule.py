"""Email template for headless DSA alerts to Main Administrators, and the 5-minute evaluation task (Celery Beat)."""

from django.db import migrations

TEMPLATE = {
    "code": "dsa_headless_alert_admin",
    "name": "Department Sync Agent alert (Main Administrator)",
    "subject": "[IIC Booking] Sync agent {{ agent_name }} ({{ department_name }}): {{ alert_title }}",
    "body_text": (
        "Dear {{ recipient_name }},\n\n"
        "{{ level_label }}: the Department Sync Agent {{ agent_name }} ({{ department_name }}) needs attention "
        "({{ time }}).\n\n{{ items_text }}\n\n"
        "The agent runs unattended; open the fleet view to see its diagnostics and send a remote action "
        "(restart, re-sync, retry uploads, support bundle):\n{{ link }}\n\n"
        "You will be reminded while the problem lasts. Acknowledge it on the dashboard to pause reminders.\n\n"
        "Regards,\nIIC Booking Team\n"
    ),
    "body_html": (
        "<p>Dear <b>{{ recipient_name }}</b>,</p>"
        "<p><b>{{ level_label }}</b>: the Department Sync Agent <b>{{ agent_name }}</b> ({{ department_name }}) "
        "needs attention ({{ time }}).</p>{{ items_html }}"
        "<p>The agent runs unattended; open the fleet view to see its diagnostics and send a remote action "
        "(restart, re-sync, retry uploads, support bundle).</p>"
        '<p><a href="{{ link }}">Open Department Sync fleet</a></p>'
        '<p style="color:#666;font-size:12px">Sent by the IIC portal because you are a Main Administrator. '
        "You will be reminded while the problem lasts; acknowledge it on the dashboard to pause reminders.</p>"
    ),
    "description": "Sent to Main Administrators when a Department Sync Agent or one of its Equipment PCs is offline, "
    "uploads are stuck, disk is low, the Equipment Network is down, the agent token is invalid or errors spike "
    "- only after the problem has lasted the grace period (default 10 minutes); repeated every 12 hours.",
    "variable_help": "recipient_name, agent_name, department_name, alert_title, level_label, items_text, items_html, "
    "time, link",
}

TASK = ("Department Sync: headless agent health and alerts (5 min)", "sync.evaluate_headless_agents")


def forwards(apps, schema_editor):
    Template = apps.get_model("communication", "CommunicationTemplate")
    Template.objects.update_or_create(
        code=TEMPLATE["code"],
        communication_type="email",
        defaults={k: v for k, v in TEMPLATE.items() if k != "code"} | {"is_active": True},
    )
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    interval, _ = IntervalSchedule.objects.get_or_create(every=5, period="minutes")
    if not PeriodicTask.objects.filter(task=TASK[1]).exists():
        PeriodicTask.objects.create(name=TASK[0], task=TASK[1], interval=interval, enabled=True)


def backwards(apps, schema_editor):
    apps.get_model("communication", "CommunicationTemplate").objects.filter(
        code=TEMPLATE["code"], communication_type="email"
    ).delete()
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(task=TASK[1]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sync", "0020_dsa_headless_observability"),
        ("communication", "0065_istem_otp_and_external_notice_templates"),
        ("django_celery_beat", "0018_improve_crontab_helptext"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
