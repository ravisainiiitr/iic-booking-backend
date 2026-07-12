# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0004_add_department_supervisor"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="phone_number",
            field=models.CharField(
                blank=True,
                help_text="Contact phone number",
                max_length=20,
                null=True,
                verbose_name="Phone Number",
            ),
        ),
    ]

