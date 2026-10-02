import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0125_supervisor_invite"),
    ]

    operations = [
        migrations.CreateModel(
            name="MobileDeviceSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("device_id", models.CharField(db_index=True, max_length=128)),
                ("device_name", models.CharField(blank=True, default="", max_length=100)),
                ("platform", models.CharField(choices=[("android", "Android"), ("ios", "iOS")], max_length=16)),
                ("app_version", models.CharField(blank=True, default="", max_length=32)),
                ("access_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("access_expires_at", models.DateTimeField()),
                ("prev_access_hash", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("prev_access_valid_until", models.DateTimeField(blank=True, null=True)),
                ("refresh_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("prev_refresh_hash", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("refreshed_at", models.DateTimeField(blank=True, null=True)),
                ("refresh_expires_at", models.DateTimeField()),
                ("absolute_expires_at", models.DateTimeField()),
                ("require_biometric", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("last_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("revoke_reason", models.CharField(blank=True, default="", max_length=32)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mobile_device_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Mobile device session",
                "verbose_name_plural": "Mobile device sessions",
                "ordering": ["-created_at"],
            },
        ),
    ]
