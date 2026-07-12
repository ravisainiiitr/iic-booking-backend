# Revert inactivity timeout back to seconds

from django.db import migrations, models


def minutes_to_seconds(apps, schema_editor):
    AuthSettings = apps.get_model("users", "AuthSettings")
    UserTypeInactivityTimeout = apps.get_model("users", "UserTypeInactivityTimeout")
    for obj in AuthSettings.objects.all():
        mins = getattr(obj, "inactivity_timeout_minutes", 30) or 30
        obj.inactivity_timeout_seconds = max(60, mins * 60)
        obj.save(update_fields=["inactivity_timeout_seconds"])
    for obj in UserTypeInactivityTimeout.objects.all():
        mins = getattr(obj, "inactivity_timeout_minutes", 30) or 30
        obj.inactivity_timeout_seconds = max(60, mins * 60)
        obj.save(update_fields=["inactivity_timeout_seconds"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0051_inactivity_timeout_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="authsettings",
            name="inactivity_timeout_seconds",
            field=models.PositiveIntegerField(
                default=1800,
                help_text="Seconds of inactivity after which the user is automatically logged out (default 1800 = 30 minutes).",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="usertypeinactivitytimeout",
            name="inactivity_timeout_seconds",
            field=models.PositiveIntegerField(
                default=1800,
                help_text="Seconds of inactivity after which this user type is automatically logged out (e.g. 1800 = 30 min).",
            ),
            preserve_default=False,
        ),
        migrations.RunPython(minutes_to_seconds, noop),
        migrations.RemoveField(
            model_name="authsettings",
            name="inactivity_timeout_minutes",
        ),
        migrations.RemoveField(
            model_name="usertypeinactivitytimeout",
            name="inactivity_timeout_minutes",
        ),
    ]
