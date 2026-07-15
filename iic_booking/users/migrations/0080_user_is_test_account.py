from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0079_sric_grant_code_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="is_test_account",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "When enabled, bookings and wallet activity for this user are excluded from revenue "
                    "reports, emails are redirected to the test inbox, and data can be wiped before go-live."
                ),
                verbose_name="Test account",
            ),
        ),
    ]
