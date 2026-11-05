from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("calls", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="callsession",
            name="role",
            field=models.CharField(
                choices=[
                    ("operator", "Lab Operator"),
                    ("oic", "Officer In Charge"),
                    ("accounts", "Accounts In Charge"),
                    ("user", "Booking user"),
                ],
                max_length=20,
            ),
        ),
    ]
