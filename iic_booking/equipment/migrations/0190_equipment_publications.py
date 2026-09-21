# Generated manually — EquipmentPublication rows (countable references)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0189_dynamicinputfield_user_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentPublication",
            fields=[
                ("equipment_publication_id", models.AutoField(primary_key=True, serialize=False)),
                ("title", models.CharField(help_text="Publication title or short label.", max_length=500, verbose_name="Title")),
                ("citation", models.TextField(blank=True, default="", help_text="Full citation text (authors, journal, year, DOI, etc.).", verbose_name="Citation")),
                ("url", models.CharField(blank=True, default="", help_text="Optional link to the publication (DOI, journal page, PDF).", max_length=500, verbose_name="URL")),
                ("year", models.PositiveIntegerField(blank=True, help_text="Publication year, if known.", null=True, verbose_name="Year")),
                ("display_order", models.PositiveIntegerField(default=0, help_text="Lower numbers appear first on the equipment page.", verbose_name="Display order")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="equipment_publications",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment publication",
                "verbose_name_plural": "Equipment publications",
                "ordering": ["display_order", "-year", "title", "equipment_publication_id"],
            },
        ),
    ]
