# Weekly view controls: time range and max rows (admin and OIC can edit)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0059_oic_monthly_report_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="weekly_view_time_from",
            field=models.TimeField(
                blank=True,
                help_text="Only show slots starting at or after this time (24h). Leave empty for no limit. Admin and OIC can edit.",
                null=True,
                verbose_name="Weekly view time from",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="weekly_view_time_to",
            field=models.TimeField(
                blank=True,
                help_text="Only show slots ending at or before this time (24h). Leave empty for no limit. Admin and OIC can edit.",
                null=True,
                verbose_name="Weekly view time to",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="weekly_view_max_rows",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Maximum number of time/slot rows to show in the weekly view. Leave empty for no limit. Admin and OIC can edit.",
                null=True,
                verbose_name="Weekly view max rows",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="weekly_view_default_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                default=7,
                help_text="Default number of days to show in the weekly view (e.g. 7 for one week). Admin and OIC can edit.",
                null=True,
                verbose_name="Weekly view default days",
            ),
        ),
    ]
