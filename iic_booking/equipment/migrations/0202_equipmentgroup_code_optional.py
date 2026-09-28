from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0201_close_pending_user_repeat_sample_requests"),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipmentgroup",
            name="code",
            field=models.CharField(
                blank=True,
                help_text="Not used; groups are identified by name.",
                max_length=255,
                null=True,
                unique=True,
                verbose_name="Legacy group code",
            ),
        ),
        migrations.AlterField(
            model_name="equipmentgroup",
            name="name",
            field=models.CharField(
                help_text="Name of the equipment group (identifies the group; must be unique)",
                max_length=255,
                verbose_name="Group Name",
            ),
        ),
    ]
