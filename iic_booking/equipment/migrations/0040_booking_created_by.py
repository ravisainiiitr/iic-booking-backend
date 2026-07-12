# Add created_by to Booking to track who created each booking (admin, OIC, or user)

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0039_bookingsampletracereplyattachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                help_text="User who created this booking (admin, officer in charge, or the booking user)",
                null=True,
                on_delete=models.SET_NULL,
                related_name="bookings_created",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by",
            ),
        ),
    ]
