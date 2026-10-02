from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0218_equipment_contact_honorific"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="allow_multiple_sample_sets",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When checked, users can add samples with different parameters (extra sample sets) to one "
                    "booking; each set is charged and timed separately. When unchecked, the option is hidden and "
                    "new bookings and templates may hold only one sample set. Existing bookings keep their sets. "
                    "Only the main administrator can change this."
                ),
                verbose_name="Allow samples with different parameters",
            ),
        ),
    ]
