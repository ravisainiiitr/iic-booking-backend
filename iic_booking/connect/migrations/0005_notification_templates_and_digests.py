"""Email templates for Connect notifications (create-only) and the daily / weekly digest schedules.

Templates carry only the thread title, community and a link: never post bodies. All values are HTML-escaped by
the sender before substitution. The digest tasks return immediately while IITR_CONNECT_ENABLED is off.
"""

from django.db import migrations

DAILY_TASK = "IITR Connect: daily discussion digest (08:05 IST)"
WEEKLY_TASK = "IITR Connect: weekly discussion digest (Monday 08:10 IST)"


def _html(title: str, subtitle: str, inner: str) -> str:
    try:
        from iic_booking.communication.email_branding import wrap_email_html

        return wrap_email_html(title=title, subtitle=subtitle, body_inner_html=inner, preheader=subtitle or title)
    except Exception:  # noqa: BLE001 - fall back to plain markup if the branding helper changes
        return f"<h2>{title}</h2><p>{subtitle}</p>{inner}"


TEMPLATES = [
    {
        "code": "connect_activity_email",
        "name": "IITR Connect: activity notification",
        "subject": "IITR Connect: {{ headline }}",
        "body_text": "Dear {{ user_name }},\n\n{{ headline }}\n{{ summary }}\n\nOpen it in IITR Connect: {{ link_url }}\n\n"
        "You can choose immediate, daily, weekly or no email in IITR Connect > Notification settings.",
        "inner": "<p>Dear {{ user_name }},</p><p><strong>{{ headline }}</strong><br>{{ summary }}</p>"
        '<p><a href="{{ link_url }}" style="display:inline-block;padding:10px 18px;background:#1e3a8a;color:#ffffff;'
        'border-radius:8px;text-decoration:none">Open in IITR Connect</a></p>'
        '<p style="color:#64748b;font-size:12px">The discussion is private to its community, so it opens only after you sign in. '
        "You can choose immediate, daily, weekly or no email in IITR Connect notification settings.</p>",
        "description": "Sent when someone replies, mentions you, accepts your answer, or posts an announcement or article you follow.",
        "variable_help": "user_name, headline, summary, link_url (all pre-escaped)",
    },
    {
        "code": "connect_digest_email",
        "name": "IITR Connect: discussion digest",
        "subject": "IITR Connect: {{ count }} updates in your {{ period }} digest",
        "body_text": "Dear {{ user_name }},\n\nHere is what happened in your IITR Connect communities:\n\n{{ items_text }}\n\n"
        "Open IITR Connect: {{ link_url }}",
        "inner": "<p>Dear {{ user_name }},</p><p>Here is what happened in your IITR Connect communities:</p>{{ items_html }}"
        '<p><a href="{{ link_url }}">Open IITR Connect</a></p>'
        '<p style="color:#64748b;font-size:12px">Only discussions you can still open are listed. '
        "Change how often you get this in IITR Connect notification settings.</p>",
        "description": "Daily or weekly summary of IITR Connect activity (titles and links only).",
        "variable_help": "user_name, period, count, items_html, items_text, link_url (all pre-escaped)",
    },
]


def create(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    for spec in TEMPLATES:
        if CommunicationTemplate.objects.filter(code=spec["code"]).exists() or CommunicationTemplate.objects.filter(name=spec["name"]).exists():
            continue
        CommunicationTemplate.objects.create(
            code=spec["code"],
            communication_type="email",
            name=spec["name"],
            subject=spec["subject"],
            body_text=spec["body_text"],
            body_html=_html("IITR Connect", "Connect. Discuss. Share Knowledge. Solve Together.", spec["inner"]),
            description=spec["description"],
            variable_help=spec["variable_help"],
            is_active=True,
        )
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    daily, _ = CrontabSchedule.objects.get_or_create(
        minute="5", hour="8", day_of_week="*", day_of_month="*", month_of_year="*", timezone="Asia/Kolkata"
    )
    weekly, _ = CrontabSchedule.objects.get_or_create(
        minute="10", hour="8", day_of_week="1", day_of_month="*", month_of_year="*", timezone="Asia/Kolkata"
    )
    if not PeriodicTask.objects.filter(name=DAILY_TASK).exists():
        PeriodicTask.objects.create(name=DAILY_TASK, task="connect.daily_digest", crontab=daily, enabled=True)
    if not PeriodicTask.objects.filter(name=WEEKLY_TASK).exists():
        PeriodicTask.objects.create(name=WEEKLY_TASK, task="connect.weekly_digest", crontab=weekly, enabled=True)


def remove(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[DAILY_TASK, WEEKLY_TASK]).delete()
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code__in=[t["code"] for t in TEMPLATES], updated_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("connect", "0004_article_reviewed_version"),
        ("communication", "0065_istem_otp_and_external_notice_templates"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(create, remove)]
