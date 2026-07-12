from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0057_organizationrequest_web_page"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="verification_email_sent_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Timestamp of the last registration verification email sent. Used to enforce verification link expiry.",
                verbose_name="Verification Email Sent At",
            ),
        ),
    ]

