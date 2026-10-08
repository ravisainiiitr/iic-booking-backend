"""DisruptionEvent soft delete: is_deleted / deleted_at / deleted_by / delete_reason (additive)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('equipment', '0239_dailyslot_released_by_booking_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='disruptionevent',
            name='is_deleted',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='deleted_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='delete_reason',
            field=models.TextField(blank=True, default=''),
        ),
    ]
