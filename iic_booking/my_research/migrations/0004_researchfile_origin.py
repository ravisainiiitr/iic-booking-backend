# Additive only: one column with a default; existing rows become "upload".

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("my_research", "0003_research_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchfile",
            name="origin",
            field=models.CharField(
                choices=[
                    ("upload", "Upload"),
                    ("booking_raw", "Booking raw data"),
                    ("analysis_output", "Analysis output"),
                ],
                default="upload",
                max_length=20,
            ),
        ),
    ]
