# Auth settings (admin-controlled inactivity timeout)

from django.db import migrations, models


def create_auth_settings_singleton(apps, schema_editor):
    AuthSettings = apps.get_model("users", "AuthSettings")
    AuthSettings.objects.get_or_create(
        pk=1,
        defaults={"inactivity_timeout_seconds": 1800},
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0048_userloginlock"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuthSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "inactivity_timeout_seconds",
                    models.PositiveIntegerField(
                        default=1800,
                        help_text="Seconds of inactivity after which the user is automatically logged out (default 1800 = 30 minutes).",
                    ),
                ),
            ],
            options={
                "db_table": "users_authsettings",
                "verbose_name": "Auth settings",
                "verbose_name_plural": "Auth settings",
            },
        ),
        migrations.RunPython(create_auth_settings_singleton, noop),
    ]
