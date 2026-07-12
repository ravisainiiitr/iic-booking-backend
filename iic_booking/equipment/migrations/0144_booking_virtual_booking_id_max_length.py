# Widen virtual_booking_id for department code + equipment code prefix

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0143_booking_payment_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="booking",
            name="virtual_booking_id",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Display ID: internal department code + equipment code + year + 5-digit sequence "
                    "(e.g. CHGEM202600001)"
                ),
                max_length=64,
                null=True,
                unique=True,
                verbose_name="Virtual Booking ID",
            ),
        ),
    ]
