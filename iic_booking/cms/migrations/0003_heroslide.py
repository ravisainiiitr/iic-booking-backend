# Generated migration for HeroSlide model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0002_seed_defaults"),
    ]

    operations = [
        migrations.CreateModel(
            name="HeroSlide",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "image",
                    models.ImageField(
                        help_text="Background image for hero carousel. Recommended: landscape, high resolution.",
                        upload_to="cms/hero/%Y/%m/",
                        verbose_name="Image",
                    ),
                ),
                (
                    "alt_text",
                    models.CharField(
                        blank=True,
                        help_text="Short description for accessibility (e.g. 'Laboratory equipment').",
                        max_length=255,
                        verbose_name="Alt text",
                    ),
                ),
                (
                    "order",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Lower number = shown first. Use to control slide order.",
                        verbose_name="Order",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Active")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["order", "id"],
                "verbose_name": "Hero background image",
                "verbose_name_plural": "Hero background images",
            },
        ),
    ]
