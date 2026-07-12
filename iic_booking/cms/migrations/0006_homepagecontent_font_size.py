# Add optional font_size to HomePageContent for per-key text sizing

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0005_cmspage_menuitem_page"),
    ]

    operations = [
        migrations.AddField(
            model_name="homepagecontent",
            name="font_size",
            field=models.CharField(
                blank=True,
                help_text="Optional CSS font size for this text (e.g. 16px, 1.2rem, 120%). Leave blank for default.",
                max_length=30,
                null=True,
                verbose_name="Font size",
            ),
        ),
    ]
