from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0055_organizationrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="organization_request",
            field=models.ForeignKey(
                blank=True,
                help_text="For Govt R&D: set when user signed up with a requested organization pending admin approval.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="users",
                to="users.organizationrequest",
                verbose_name="Pending organization request",
            ),
        ),
    ]
