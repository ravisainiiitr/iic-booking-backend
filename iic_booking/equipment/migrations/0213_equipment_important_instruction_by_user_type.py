from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0211_next_week_slots_wednesday_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="important_instruction_by_user_type",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    'Optional instruction per user type code (e.g. {"student": "..."}). '
                    "User types without an entry see the default important instruction."
                ),
                verbose_name="Important instruction per user type",
            ),
        ),
    ]
