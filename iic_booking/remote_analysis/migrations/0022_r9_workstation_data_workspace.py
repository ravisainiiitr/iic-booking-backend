# Generated for Phase R9 — production numbering (prod already has 0017–0021)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0021_retire_direct_rdp_transport"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisworkstation",
            name="cleanup_status",
            field=models.CharField(blank=True, default="idle", max_length=32),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="data_root",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="disk_low",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="input_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="input_path",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="last_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="output_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="output_path",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="analysisworkstation",
            name="workspace_disk_free_bytes",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
