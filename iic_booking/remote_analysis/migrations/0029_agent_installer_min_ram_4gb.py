from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0028_browse_pc_folders_command"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agentinstallerrelease",
            name="min_ram_gb",
            field=models.PositiveIntegerField(default=4),
        ),
    ]
