from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0122_walletsricsettings_mode_switches"),
    ]

    operations = [
        migrations.AlterField(
            model_name="walletrechargerequest",
            name="rejection_reason_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wrong_project_grant", "Wrong Project Code"),
                    ("insufficient_balance", "Insufficient Funds in the Project"),
                    ("project_closed", "Project Already Closed"),
                    ("mismatch_user_info", "Mismatch in User Information"),
                    ("other", "Other"),
                ],
                help_text="Predefined rejection reason when status is Rejected",
                max_length=40,
                verbose_name="Rejection Reason Code",
            ),
        ),
    ]
