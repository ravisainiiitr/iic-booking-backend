# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0005_add_phone_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="profile_picture",
            field=models.ImageField(
                blank=True,
                help_text="User profile picture",
                null=True,
                upload_to="profile_pictures/",
                verbose_name="Profile Picture",
            ),
        ),
    ]

