# Generated manually for Omniport joining / graduation dates on User

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0088_wallet_recharge_approval_workflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="joining_date",
            field=models.DateField(
                blank=True,
                help_text="Institute joining / programme start date for faculty or students (from Omniport role start_date).",
                null=True,
                verbose_name="Joining Date",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="graduation_date",
            field=models.DateField(
                blank=True,
                help_text="Expected or actual graduation / programme end date for students (from Omniport student end_date).",
                null=True,
                verbose_name="Graduation Date",
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="designation",
            field=models.CharField(
                blank=True,
                help_text="Designation for faculty members (from Omniport / Channel i)",
                max_length=255,
                null=True,
                verbose_name="Designation",
            ),
        ),
    ]
