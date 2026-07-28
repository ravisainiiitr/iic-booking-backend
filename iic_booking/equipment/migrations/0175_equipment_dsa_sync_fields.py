from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Experimental DSA fields on Equipment from the preliminary sync-agent API.

    Reparented onto the main equipment migration chain (was previously an
    orphan leaf at 0112 that conflicted with 0112_archive_samples_schedule).
    Long-term sync configuration lives in sync.EquipmentSyncProfile; these
    columns remain for compatibility with the existing /api/sync-agent/instruments
    path until a later cutover.
    """

    dependencies = [
        ("equipment", "0174_labusercalendarcolorpreference"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="dsa_hostname",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Instrument PC hostname published to the Department Sync Agent.",
                max_length=200,
                verbose_name="DSA hostname",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="dsa_ip_address",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Instrument PC LAN IP published to the Department Sync Agent.",
                max_length=64,
                verbose_name="DSA IP address",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="dsa_share_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="SMB share name on the instrument PC (e.g. Results).",
                max_length=200,
                verbose_name="DSA share name",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="dsa_unc_path",
            field=models.CharField(
                blank=True,
                default="",
                help_text=r"Full UNC path, e.g. \\192.168.1.2\Results. Auto-built from IP + share when blank.",
                max_length=500,
                verbose_name="DSA UNC path",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="dsa_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When unchecked, this equipment is not published to the Department Sync Agent.",
                verbose_name="DSA enabled",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="dsa_watch_folder_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When enabled, the Sync Agent may attach file watchers for this instrument.",
                verbose_name="DSA watch folder enabled",
            ),
        ),
    ]
