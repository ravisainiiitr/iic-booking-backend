from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0084_remove_user_oic_enable_multi_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="user_guide_viewed",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True after the user completes or dismisses the role-specific onboarding "
                    "user guide. They can still reopen the guide from the Help menu anytime."
                ),
                verbose_name="User guide viewed",
            ),
        ),
    ]
