import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TASK_NAME = "Cancel rejected fabrication bookings after the replace window (every 10 minutes)"


def create_expire_fabrication_rejections_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_10_minutes, _ = IntervalSchedule.objects.get_or_create(every=10, period="minutes")
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="equipment.expire_fabrication_rejections",
            interval=every_10_minutes,
            enabled=True,
        )


def remove_expire_fabrication_rejections_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0229_fabrication_notification_emails_data'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='fabrication_rejected_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Fabrication files rejected at'),
        ),
        migrations.AddField(
            model_name='booking',
            name='fabrication_rejected_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bookings_fabrication_rejected', to=settings.AUTH_USER_MODEL, verbose_name='Fabrication files rejected by'),
        ),
        migrations.AddField(
            model_name='booking',
            name='fabrication_rejection_reason',
            field=models.TextField(blank=True, default='', verbose_name='Fabrication rejection reason'),
        ),
        migrations.AddField(
            model_name='booking',
            name='fabrication_replace_deadline',
            field=models.DateTimeField(blank=True, db_index=True, help_text='After this time a rejected fabrication booking is cancelled with a full refund.', null=True, verbose_name='Replace files by'),
        ),
        migrations.AddField(
            model_name='equipment',
            name='fabrication_replace_window_hours',
            field=models.PositiveSmallIntegerField(default=24, help_text='For 3D printing and 2D laser cutting equipment: after the lab rejects a booking as not feasible, the user has this many hours to upload new files. Otherwise the booking is cancelled with a full refund.', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(168)], verbose_name='Hours to replace rejected files'),
        ),
        migrations.RunPython(
            create_expire_fabrication_rejections_schedule, remove_expire_fabrication_rejections_schedule
        ),
    ]
