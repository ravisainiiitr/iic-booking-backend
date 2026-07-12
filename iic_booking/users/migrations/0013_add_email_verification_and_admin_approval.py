# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0012_alter_masterlookup_category_delete_supervisor"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_verified",
            field=models.BooleanField(
                default=False,
                help_text="Whether the user's email address has been verified",
                verbose_name="Email Verified",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="admin_approved",
            field=models.BooleanField(
                default=False,
                help_text="Whether the user has been approved by an administrator",
                verbose_name="Admin Approved",
            ),
        ),
    ]

