"""Shared helpers for django-celery-beat CrontabSchedule creation in migrations.

Historical models from early django_celery_beat migrations do not include
``timezone``. Live django-celery-beat 2.x does. Always gate on field presence
and prefer depending on beat >= 0016 when Asia/Kolkata must be set.
"""


def crontab_get_or_create(
    CrontabSchedule,
    *,
    minute,
    hour,
    day_of_week="*",
    day_of_month="*",
    month_of_year="*",
    timezone="Asia/Kolkata",
):
    kwargs = {
        "minute": minute,
        "hour": hour,
        "day_of_week": day_of_week,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
    }
    field_names = {getattr(f, "name", None) for f in CrontabSchedule._meta.fields}
    if "timezone" in field_names and timezone is not None:
        kwargs["timezone"] = timezone
    return CrontabSchedule.objects.get_or_create(**kwargs)
