# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="user_type",
            field=models.CharField(
                choices=[
                    ("admin", "Admin"),
                    ("student", "Student"),
                    ("faculty", "Faculty"),
                    ("external", "External"),
                    ("manager", "Manager"),
                    ("operator", "Operator"),
                    ("finance", "Finance"),
                    ("type_8", "Type 8"),
                    ("type_9", "Type 9"),
                    ("type_10", "Type 10"),
                ],
                default="external",
                help_text="Type of user in the system",
                max_length=20,
                verbose_name="User Type",
            ),
        ),
    ]

