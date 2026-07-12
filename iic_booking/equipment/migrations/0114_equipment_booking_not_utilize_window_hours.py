from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0113_sample_trace_not_utilized_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="booking_not_utilize_window_hours",
            field=models.PositiveIntegerField(
                default=24,
                help_text='After booking end time, if sample lifecycle has no update or only "Sample Sent" for this many hours, booking is auto-marked as Booking Not Utilized. Set to 0 to disable for this equipment.',
                verbose_name="Booking Not Utilize Window (hours)",
            ),
        ),
    ]

