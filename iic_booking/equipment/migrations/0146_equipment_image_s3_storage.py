from django.db import migrations, models

from iic_booking.equipment.models import equipment_image_upload_to
from iic_booking.storage_backends import EquipmentImageStorage


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0145_equipment_make_and_completion_email_extra"),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipment",
            name="image",
            field=models.ImageField(
                blank=True,
                help_text="Photo shown when booking. Stored in S3 (media/equipment_images/) until replaced.",
                max_length=512,
                null=True,
                storage=EquipmentImageStorage(),
                upload_to=equipment_image_upload_to,
                verbose_name="Equipment image",
            ),
        ),
    ]
