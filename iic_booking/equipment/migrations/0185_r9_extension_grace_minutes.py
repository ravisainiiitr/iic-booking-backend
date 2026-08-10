# Generated for Phase R9 — production numbering (prod already has 0183–0184)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0184_equipment_analysis_checkin_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="analysis_extension_grace_minutes",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Optional one-shot short extension when another user is waiting. 0 = deny (default fair-access policy).",
                verbose_name="Extension grace when others are waiting (minutes)",
            ),
        ),
    ]
