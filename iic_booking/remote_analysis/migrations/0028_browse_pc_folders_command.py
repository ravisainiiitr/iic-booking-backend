from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0027_ra_workspace_research_link"),
    ]

    operations = [
        migrations.AlterField(
            model_name="remotecommand",
            name="command_type",
            field=models.CharField(
                choices=[
                    ("PING", "Ping"),
                    ("REFRESH", "Refresh"),
                    ("REFRESH_SOFTWARE", "Refresh software"),
                    ("COLLECT_LOGS", "Collect logs"),
                    ("RESTART_AGENT", "Restart agent"),
                    ("PREPARE_WORKSTATION", "Prepare workstation"),
                    ("CLEAN_WORKSTATION", "Clean workstation"),
                    ("SYNC_WORKSPACE", "Synchronize analysis workspace"),
                    ("COLLECT_WORKSPACE", "Collect workspace outputs"),
                    ("JOIN_TUNNEL", "Join reverse tunnel"),
                    ("CLOSE_TUNNEL", "Close reverse tunnel"),
                    ("BROWSE_PC_FOLDERS", "Browse folders on the Analysis PC"),
                ],
                max_length=64,
            ),
        ),
    ]
