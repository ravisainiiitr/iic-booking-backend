# Add editing_required to DynamicInputField (allow user to edit field after booking until Complete)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0028_ensure_bookingresultfile_table"),
    ]

    operations = [
        migrations.AddField(
            model_name="dynamicinputfield",
            name="editing_required",
            field=models.BooleanField(
                default=False,
                help_text="If checked, this field can be edited by the user after booking until status is Complete.",
            ),
        ),
    ]
