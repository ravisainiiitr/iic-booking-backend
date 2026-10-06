from django.db import migrations, models

TASK_NAME = "Faculty legacy wallet daily sync (02:30 IST)"


def create_daily_sync_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="2",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="users.faculty_wallet_daily_sync",
            crontab=crontab,
            enabled=True,
        )


def remove_daily_sync_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    """Last batch sync status on PortalMigrationState, plus the daily 02:30 IST beat entry.

    The task itself checks the Main Administrator deadline, so the schedule can stay enabled.
    """

    dependencies = [
        ("users", "0131_faculty_wallet_sync_cutoff"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalmigrationstate",
            name="faculty_wallet_last_batch_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="faculty_wallet_last_batch_sync_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(create_daily_sync_schedule, remove_daily_sync_schedule),
    ]
