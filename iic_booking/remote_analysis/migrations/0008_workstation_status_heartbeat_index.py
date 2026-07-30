# Generated manually for WS4 offline-sweep index

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0007_production_hardening_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="analysisworkstation",
            index=models.Index(fields=["status", "last_heartbeat"], name="ra_ws_status_hb_idx"),
        ),
    ]
