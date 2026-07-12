import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0099_merge_20260324_0115"),
    ]

    operations = [
        migrations.AddField(
            model_name="tarewardconfig",
            name="equipment",
            field=models.OneToOneField(
                blank=True,
                help_text="If set, this config applies to the specific equipment.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ta_reward_config",
                to="equipment.equipment",
                verbose_name="Equipment",
            ),
        ),
    ]

