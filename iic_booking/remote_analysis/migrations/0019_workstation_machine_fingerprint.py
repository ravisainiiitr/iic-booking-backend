from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0018_analysis_pc_maintenance_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisworkstation",
            name="machine_guid",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="bios_uuid",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="machine_fingerprint",
            field=models.CharField(blank=True, db_index=True, default="", max_length=256),
        ),
    ]
