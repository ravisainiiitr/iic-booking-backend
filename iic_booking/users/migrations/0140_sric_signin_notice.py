from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0139_account_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="sricwalletrechargesettings",
            name="signin_notice_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When a recharge arrives for an Employee ID with no portal account (the faculty member has not yet "
                    "signed in with Channel i), email them once asking them to sign in to the new portal, with a link to it."
                ),
                verbose_name="Email faculty who have not signed in yet",
            ),
        ),
        migrations.AddField(
            model_name="sricwalletrecharge",
            name="signin_notice_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Sign-in reminder sent at"),
        ),
        migrations.AddField(
            model_name="sricwalletrecharge",
            name="signin_notice_email",
            field=models.CharField(blank=True, max_length=255, verbose_name="Sign-in reminder sent to"),
        ),
    ]
