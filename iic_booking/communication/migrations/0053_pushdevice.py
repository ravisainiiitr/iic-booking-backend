# Generated manually for PushDevice (Android/iOS FCM registration)

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("communication", "0052_refresh_iit_roorkee_email_branding"),
    ]

    operations = [
        migrations.CreateModel(
            name="PushDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(db_index=True, max_length=512, verbose_name="Device Token")),
                (
                    "platform",
                    models.CharField(
                        choices=[("android", "Android"), ("ios", "iOS"), ("web", "Web")],
                        db_index=True,
                        default="android",
                        max_length=16,
                        verbose_name="Platform",
                    ),
                ),
                ("device_name", models.CharField(blank=True, max_length=255, verbose_name="Device Name")),
                ("app_version", models.CharField(blank=True, max_length=64, verbose_name="App Version")),
                ("is_active", models.BooleanField(db_index=True, default=True, verbose_name="Active")),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="Last Seen")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="push_devices",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Push Device",
                "verbose_name_plural": "Push Devices",
            },
        ),
        migrations.AddConstraint(
            model_name="pushdevice",
            constraint=models.UniqueConstraint(fields=("user", "token"), name="uniq_push_device_user_token"),
        ),
        migrations.AddIndex(
            model_name="pushdevice",
            index=models.Index(fields=["user", "is_active"], name="communicati_user_id_push_idx"),
        ),
        migrations.AddIndex(
            model_name="pushdevice",
            index=models.Index(fields=["platform", "is_active"], name="communicati_platfor_push_idx"),
        ),
    ]
