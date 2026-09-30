from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0209_alter_equipment_sample_submission_lead_hours"),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipmentoperator",
            name="disable_booking_confirmation_email",
            field=models.BooleanField(
                default=False,
                help_text="When checked, this Lab Operator does not receive booking confirmation emails for this equipment.",
            ),
        ),
        migrations.AlterField(
            model_name="equipmentoperator",
            name="office_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Office / lab address of this Lab Operator for enquiries about this equipment",
                verbose_name="Office address",
            ),
        ),
    ]
