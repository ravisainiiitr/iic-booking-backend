from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0120_user_email_login_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="project_grant_recharge_enabled",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    "When off, faculty cannot raise new Project Grant recharge requests (or send unsent ones to the "
                    "SRIC Office). Direct Cash Deposit / Bank Transfer is unaffected."
                ),
                verbose_name="Allow wallet recharge requests via Project Grant",
            ),
        ),
    ]
