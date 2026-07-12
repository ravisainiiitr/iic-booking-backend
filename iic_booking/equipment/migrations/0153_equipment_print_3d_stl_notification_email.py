from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0152_equipment_istem_fbr_status_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="print_3d_stl_notification_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="For 3D printing equipment only: when a booking is confirmed, the user's STL file(s) and booking details are emailed to this address. Leave blank to disable.",
                max_length=254,
                verbose_name="3D print STL notification email",
            ),
        ),
    ]
