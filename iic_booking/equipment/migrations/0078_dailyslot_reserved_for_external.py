# Generated manually for Reserved for External Users feature

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0077_equipment_user_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailyslot",
            name="reserved_for_external",
            field=models.BooleanField(
                default=False,
                help_text="When True, this slot is shown as Available to external users; only these slots can be booked by external users. Admin and OIC can mark/unmark.",
                verbose_name="Reserved for External Users",
            ),
        ),
    ]
