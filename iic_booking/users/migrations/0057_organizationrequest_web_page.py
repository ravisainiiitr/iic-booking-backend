from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0056_user_organization_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationrequest",
            name="web_page",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Web page URL entered by the requester (optional).",
                verbose_name="Organization web page",
            ),
        ),
    ]

