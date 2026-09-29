from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0205_equipmentmanager_equipmentoperator_contact"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="auto_allocate_alternative_default",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    'Initial state of "Automatically search and allocate alternate equipment" on the booking page. '
                    "Off (default): if this equipment has no free slot, the user is shown the alternate equipment of "
                    "the same group and asked to confirm before anything is booked. On: the option starts ticked and "
                    "the booking is allocated on the alternate automatically. The user can change it on each booking. "
                    "Applies only when the equipment group offers alternatives."
                ),
                verbose_name="Auto-allocate alternate equipment by default",
            ),
        ),
    ]
