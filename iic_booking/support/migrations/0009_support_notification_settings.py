import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DEFAULT_TICKET_ALERT_EMAILS = "ravisaini.15@gmail.com"


def seed_singleton(apps, schema_editor):
    SupportNotificationSettings = apps.get_model("support", "SupportNotificationSettings")
    SupportNotificationSettings.objects.get_or_create(
        pk=1,
        defaults={"ticket_alert_emails": DEFAULT_TICKET_ALERT_EMAILS, "ticket_alert_enabled": True},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0008_portal_feedback"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupportNotificationSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "ticket_alert_enabled",
                    models.BooleanField(default=True, verbose_name="Email a copy of every new support ticket"),
                ),
                (
                    "ticket_alert_emails",
                    models.TextField(
                        blank=True,
                        default=DEFAULT_TICKET_ALERT_EMAILS,
                        help_text=(
                            "Comma, semicolon or one-per-line list. Every listed address receives a copy of each new "
                            "support ticket, in addition to the OIC / assignee notifications."
                        ),
                        verbose_name="New ticket alert recipients",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Updated by",
                    ),
                ),
            ],
            options={
                "verbose_name": "Support notification settings",
                "verbose_name_plural": "Support notification settings",
            },
        ),
        migrations.RunPython(seed_singleton, migrations.RunPython.noop),
    ]
