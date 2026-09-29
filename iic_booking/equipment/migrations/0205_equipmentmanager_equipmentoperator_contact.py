from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0204_equipment_office_address_alternate_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentmanager",
            name="office_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Office address of this Officer in Charge for enquiries about this equipment",
                verbose_name="Office address",
            ),
        ),
        migrations.AddField(
            model_name="equipmentmanager",
            name="alternate_phone_number",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Extra contact number shown alongside the phone number from the user's profile",
                max_length=40,
                verbose_name="Additional phone number",
            ),
        ),
        migrations.AddField(
            model_name="equipmentoperator",
            name="office_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Office / lab address of this Lab In-charge for enquiries about this equipment",
                verbose_name="Office address",
            ),
        ),
        migrations.AddField(
            model_name="equipmentoperator",
            name="alternate_phone_number",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Extra contact number shown alongside the phone number from the user's profile",
                max_length=40,
                verbose_name="Additional phone number",
            ),
        ),
    ]
