"""Equipment.print_estimate_profile: per-printer estimate preset / overrides / calibration (additive, nullable)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0240_disruptionevent_soft_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='print_estimate_profile',
            field=models.JSONField(
                blank=True,
                default=None,
                help_text=(
                    'For 3D printing equipment: printer type preset, parameter overrides and calibration used for '
                    'the weight / time estimate of uploaded STL files. Blank uses the preset detected from Make / '
                    'Model.'
                ),
                null=True,
                verbose_name='3D print estimate profile',
            ),
        ),
    ]
