from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0160_bookingsampletrace_results_folder_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="operator_absent_hold_until",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When set by Admin/OIC, automatic Operator Absent / Operator Unavailable marking "
                    "uses the later of this time and the last booked slot end as the booking end reference. "
                    "Slots and booking schedule are not modified."
                ),
                null=True,
                verbose_name="Operator absent hold until",
            ),
        ),
    ]
