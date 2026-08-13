from django.db import migrations, models


def forwards(apps, schema_editor):
    RemoteAnalysisSettings = apps.get_model("remote_analysis", "RemoteAnalysisSettings")
    RemoteAnalysisSettings.objects.filter(analyze_data_button_label="Analyze Data").update(
        analyze_data_button_label="Open Analysis Workspace"
    )


def backwards(apps, schema_editor):
    RemoteAnalysisSettings = apps.get_model("remote_analysis", "RemoteAnalysisSettings")
    RemoteAnalysisSettings.objects.filter(
        analyze_data_button_label="Open Analysis Workspace"
    ).update(analyze_data_button_label="Analyze Data")


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0025_r11_installed_software_allocation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="remoteanalysissettings",
            name="analyze_data_button_label",
            field=models.CharField(
                default="Open Analysis Workspace",
                help_text="Default user-facing CTA label for opening Analysis Workspace.",
                max_length=128,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
