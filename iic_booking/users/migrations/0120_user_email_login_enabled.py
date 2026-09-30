from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0119_lab_operator_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_login_enabled",
            field=models.BooleanField(
                blank=True,
                default=None,
                null=True,
                help_text=(
                    "Channel i users only (IITR students, faculty, OIC, Lab Operator): allow signing in with email "
                    "(password or email OTP). Empty = default for the user type (off for students and faculty)."
                ),
                verbose_name="Email login enabled",
            ),
        ),
    ]
