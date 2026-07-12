# Store inactivity timeout in minutes instead of seconds

from django.db import migrations, models


def seconds_to_minutes(apps, schema_editor):
    AuthSettings = apps.get_model("users", "AuthSettings")
    UserTypeInactivityTimeout = apps.get_model("users", "UserTypeInactivityTimeout")
    for obj in AuthSettings.objects.all():
        sec = getattr(obj, "inactivity_timeout_seconds", 1800) or 1800
        obj.inactivity_timeout_minutes = max(1, sec // 60)
        obj.save(update_fields=["inactivity_timeout_minutes"])
    for obj in UserTypeInactivityTimeout.objects.all():
        sec = getattr(obj, "inactivity_timeout_seconds", 1800) or 1800
        obj.inactivity_timeout_minutes = max(1, sec // 60)
        obj.save(update_fields=["inactivity_timeout_minutes"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0050_add_user_type_inactivity_timeout"),
    ]

    operations = [
        migrations.AddField(
            model_name="authsettings",
            name="inactivity_timeout_minutes",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Minutes of inactivity after which the user is automatically logged out (default 30).",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="usertypeinactivitytimeout",
            name="inactivity_timeout_minutes",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Minutes of inactivity after which this user type is automatically logged out (default 30).",
            ),
            preserve_default=False,
        ),
        migrations.RunPython(seconds_to_minutes, noop),
        migrations.RemoveField(
            model_name="authsettings",
            name="inactivity_timeout_seconds",
        ),
        migrations.RemoveField(
            model_name="usertypeinactivitytimeout",
            name="inactivity_timeout_seconds",
        ),
    ]
