from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0154_equipment_model_information"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailyslot",
            name="home_department_only",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "When True, only students/faculty whose department matches this equipment’s "
                    "internal department may book this slot. Default False = any department. "
                    "Admin and OIC can mark/unmark. Has no effect if the equipment has no internal department."
                ),
                verbose_name="Home department only",
            ),
        ),
    ]
