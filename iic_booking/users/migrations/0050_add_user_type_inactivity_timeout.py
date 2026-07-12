# Per-user-type inactivity timeout

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0049_add_auth_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserTypeInactivityTimeout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "user_type",
                    models.CharField(
                        help_text="User type (e.g. student, faculty, admin). Each type can have its own timeout.",
                        max_length=50,
                        unique=True,
                    ),
                ),
                (
                    "inactivity_timeout_seconds",
                    models.PositiveIntegerField(
                        default=1800,
                        help_text="Seconds of inactivity after which this user type is automatically logged out (e.g. 1800 = 30 min).",
                    ),
                ),
            ],
            options={
                "db_table": "users_usertypeinactivitytimeout",
                "verbose_name": "User type inactivity timeout",
                "verbose_name_plural": "User type inactivity timeouts",
                "ordering": ["user_type"],
            },
        ),
    ]
