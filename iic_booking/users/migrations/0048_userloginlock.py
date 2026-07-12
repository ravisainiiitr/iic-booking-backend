# Generated manually for single-session login lock

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0047_add_user_gender"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserLoginLock",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=models.CASCADE,
                        primary_key=True,
                        related_name="+",
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "users_userloginlock",
                "verbose_name": "User login lock",
                "verbose_name_plural": "User login locks",
            },
        ),
    ]
