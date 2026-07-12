from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0125_waitlistentry_status_and_remark"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="auto_slot_selection_default",
            field=models.BooleanField(
                blank=True,
                default=None,
                help_text="Default state of the 'Auto Slot Selection' toggle on the booking page for this equipment. When unset, the user's preference is used.",
                null=True,
                verbose_name="Auto Slot Selection default (override)",
            ),
        ),
    ]

