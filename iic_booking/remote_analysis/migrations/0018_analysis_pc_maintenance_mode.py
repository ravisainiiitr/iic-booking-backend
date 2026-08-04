# Enterprise Analysis PC maintenance mode fields + status choices expansion

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0017_restore_reverse_tunnel_transport"),
    ]

    operations = [
        migrations.AddField(
            model_name="maintenancewindow",
            name="kind",
            field=models.CharField(
                choices=[
                    ("MAINTENANCE", "Scheduled maintenance"),
                    ("CALIBRATION", "Calibration"),
                    ("SOFTWARE_UPDATE", "Software update"),
                    ("HARDWARE_FAULT", "Hardware fault"),
                    ("CLEANING", "Cleaning"),
                    ("OFFLINE", "Offline"),
                    ("DISABLED", "Disabled"),
                ],
                db_index=True,
                default="MAINTENANCE",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="assigned_engineer",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="amc_reference",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="ticket_number",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="maintenance_notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="restore_status",
            field=models.CharField(
                choices=[
                    ("REGISTERING", "Registering"),
                    ("ONLINE", "Online"),
                    ("AVAILABLE", "Available"),
                    ("PREPARING", "Preparing"),
                    ("BUSY", "Busy"),
                    ("RESERVED", "Reserved"),
                    ("CLEANING", "Cleaning"),
                    ("OFFLINE", "Offline"),
                    ("MAINTENANCE", "Maintenance"),
                    ("CALIBRATION", "Calibration"),
                    ("SOFTWARE_UPDATE", "Software update"),
                    ("HARDWARE_FAULT", "Hardware fault"),
                    ("DISABLED", "Disabled"),
                    ("ERROR", "Error"),
                    ("UNKNOWN", "Unknown"),
                ],
                default="AVAILABLE",
                help_text="Status applied when the window ends (if agent still healthy).",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="previous_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="restored_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="recurrence_rule",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="maintenancewindow",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name="maintenancewindow",
            name="end",
            field=models.DateTimeField(
                help_text="Expected end — scheduler restores availability after this time."
            ),
        ),
        migrations.AlterField(
            model_name="analysisworkstation",
            name="status",
            field=models.CharField(
                choices=[
                    ("REGISTERING", "Registering"),
                    ("ONLINE", "Online"),
                    ("AVAILABLE", "Available"),
                    ("PREPARING", "Preparing"),
                    ("BUSY", "Busy"),
                    ("RESERVED", "Reserved"),
                    ("CLEANING", "Cleaning"),
                    ("OFFLINE", "Offline"),
                    ("MAINTENANCE", "Maintenance"),
                    ("CALIBRATION", "Calibration"),
                    ("SOFTWARE_UPDATE", "Software update"),
                    ("HARDWARE_FAULT", "Hardware fault"),
                    ("DISABLED", "Disabled"),
                    ("ERROR", "Error"),
                    ("UNKNOWN", "Unknown"),
                ],
                db_index=True,
                default="REGISTERING",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="workstationstatehistory",
            name="from_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REGISTERING", "Registering"),
                    ("ONLINE", "Online"),
                    ("AVAILABLE", "Available"),
                    ("PREPARING", "Preparing"),
                    ("BUSY", "Busy"),
                    ("RESERVED", "Reserved"),
                    ("CLEANING", "Cleaning"),
                    ("OFFLINE", "Offline"),
                    ("MAINTENANCE", "Maintenance"),
                    ("CALIBRATION", "Calibration"),
                    ("SOFTWARE_UPDATE", "Software update"),
                    ("HARDWARE_FAULT", "Hardware fault"),
                    ("DISABLED", "Disabled"),
                    ("ERROR", "Error"),
                    ("UNKNOWN", "Unknown"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="workstationstatehistory",
            name="to_status",
            field=models.CharField(
                choices=[
                    ("REGISTERING", "Registering"),
                    ("ONLINE", "Online"),
                    ("AVAILABLE", "Available"),
                    ("PREPARING", "Preparing"),
                    ("BUSY", "Busy"),
                    ("RESERVED", "Reserved"),
                    ("CLEANING", "Cleaning"),
                    ("OFFLINE", "Offline"),
                    ("MAINTENANCE", "Maintenance"),
                    ("CALIBRATION", "Calibration"),
                    ("SOFTWARE_UPDATE", "Software update"),
                    ("HARDWARE_FAULT", "Hardware fault"),
                    ("DISABLED", "Disabled"),
                    ("ERROR", "Error"),
                    ("UNKNOWN", "Unknown"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="maintenancewindow",
            index=models.Index(fields=["active", "start", "end"], name="ra_maint_active_window_idx"),
        ),
        migrations.AddIndex(
            model_name="maintenancewindow",
            index=models.Index(
                fields=["workstation", "active", "kind"], name="ra_maint_ws_active_kind_idx"
            ),
        ),
    ]
