# SiteDocument for public home-page PDFs (e.g. Analysis Charges)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0008_update_home_institute_branding"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "key",
                    models.CharField(
                        choices=[("analysis_charges", "Analysis Charges")],
                        db_index=True,
                        max_length=64,
                        unique=True,
                        verbose_name="Key",
                    ),
                ),
                ("title", models.CharField(blank=True, max_length=200, verbose_name="Title")),
                (
                    "document",
                    models.FileField(
                        blank=True,
                        help_text="PDF shown when visitors click the matching home-page button.",
                        null=True,
                        upload_to="cms/site_documents/%Y/%m/",
                        verbose_name="Document",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Site document",
                "verbose_name_plural": "Site documents",
                "ordering": ["key"],
            },
        ),
    ]
