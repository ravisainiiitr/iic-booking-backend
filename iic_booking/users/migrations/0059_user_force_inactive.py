from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0058_user_verification_email_sent_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="force_inactive",
            field=models.BooleanField(
                default=False,
                help_text="When True, the account is deactivated even if verification/approvals are complete.",
                verbose_name="Force Inactive",
            ),
        ),
    ]

