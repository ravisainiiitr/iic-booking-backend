# Generated manually for Channel-I gender + enrolment fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0099_channel_i_identity_architecture"),
    ]

    operations = [
        migrations.AddField(
            model_name="channeliidentityprofile",
            name="student_enrolment_number",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="channeliidentityprofile",
            name="channel_i_sex",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="channeliidentityprofile",
            name="normalized_gender",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="channeliidentityprofile",
            name="gender_locked_from_channel_i",
            field=models.BooleanField(
                default=False,
                help_text="When True, User.gender is Channel-I supplied and must not be user-edited.",
            ),
        ),
    ]
