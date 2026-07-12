# Partial indexes for PostgreSQL to speed weekly/monthly quota checks (EXISTS on daily_slots + booking filters).

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0129_equipment_skip_quota_check"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="dailyslot",
            index=models.Index(
                fields=["booking", "start_datetime"],
                name="equip_ds_booking_start_dt",
                condition=Q(booking__isnull=False),
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(
                fields=["user", "equipment", "status"],
                name="book_quota_user_eq_st",
                condition=Q(source_booking__isnull=True),
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(
                fields=["equipment", "user_type_snapshot", "status"],
                name="book_quota_eq_ut_st",
                condition=Q(source_booking__isnull=True),
            ),
        ),
    ]
