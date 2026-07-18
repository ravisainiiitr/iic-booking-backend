from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0167_equipment_enable_multi_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="sample_submission_deadline_reminder_sent_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When the advance (12 hours before sample submission deadline) "
                    "email/notification was sent. Null means not yet sent."
                ),
                null=True,
                verbose_name="Sample submission deadline reminder sent at",
            ),
        ),
    ]
