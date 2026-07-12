from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0059_user_force_inactive"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationrequest",
            name="requester_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Name of the user requesting this organization (optional).",
                max_length=255,
                verbose_name="Requester name",
            ),
        ),
    ]

