import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0057_publish_pending_equipment_notices"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("category", models.CharField(max_length=40, verbose_name="Category")),
                ("email_enabled", models.BooleanField(default=True, verbose_name="Email")),
                ("in_app_enabled", models.BooleanField(default=True, verbose_name="In-app")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_settings",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Notification preference",
                "verbose_name_plural": "Notification preferences",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "category"), name="uniq_notification_preference_user_category"
                    )
                ],
            },
        ),
    ]
