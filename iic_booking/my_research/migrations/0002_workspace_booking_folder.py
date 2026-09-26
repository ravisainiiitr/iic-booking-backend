import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("my_research", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchworkspacebooking",
            name="folder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="booking_links",
                to="my_research.researchfolder",
            ),
        ),
    ]
