# Generated for Phase R9 — production numbering

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0022_r9_workstation_data_workspace"),
    ]

    operations = [
        migrations.AddField(
            model_name="remotedesktopsession",
            name="extension_grace_used",
            field=models.BooleanField(
                default=False,
                help_text="True after a one-shot grace extension while others were waiting (R9).",
            ),
        ),
    ]
