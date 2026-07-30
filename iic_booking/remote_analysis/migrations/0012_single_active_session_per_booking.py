# Phase 3 — single active session per booking setting

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("remote_analysis", "0011_commissioning_run_observability"),
    ]

    operations = [
        migrations.AddField(
            model_name="remoteanalysissettings",
            name="single_active_session_per_booking",
            field=models.BooleanField(
                default=True,
                help_text="When True, only one open remote desktop session is allowed per booking (or reservation).",
            ),
        ),
    ]
