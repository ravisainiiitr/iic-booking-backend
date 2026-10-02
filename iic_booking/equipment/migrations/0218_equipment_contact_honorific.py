from django.db import migrations, models

HONORIFIC_CHOICES = [
    ("", "Automatic / none"),
    ("Mr.", "Mr."),
    ("Mrs.", "Mrs."),
    ("Ms.", "Ms."),
    ("Miss", "Miss"),
    ("Dr.", "Dr."),
    ("Prof.", "Prof."),
]
HONORIFIC_HELP_TEXT = (
    "Title shown before this person's name for this equipment. Leave blank to use the automatic title."
)


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0217_booking_template_preferred_slot"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentmanager",
            name="honorific",
            field=models.CharField(
                blank=True,
                choices=HONORIFIC_CHOICES,
                db_default="",
                default="",
                help_text=HONORIFIC_HELP_TEXT,
                max_length=8,
                verbose_name="Honorific",
            ),
        ),
        migrations.AddField(
            model_name="equipmentoperator",
            name="honorific",
            field=models.CharField(
                blank=True,
                choices=HONORIFIC_CHOICES,
                db_default="",
                default="",
                help_text=HONORIFIC_HELP_TEXT,
                max_length=8,
                verbose_name="Honorific",
            ),
        ),
    ]
