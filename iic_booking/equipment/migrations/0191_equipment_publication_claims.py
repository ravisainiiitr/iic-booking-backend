# Generated manually — EquipmentPublicationClaim + publication DOI/source fields

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0190_equipment_publications"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentPublicationClaim",
            fields=[
                ("claim_id", models.AutoField(primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=500, verbose_name="Title")),
                ("authors", models.CharField(blank=True, default="", max_length=1000, verbose_name="Authors")),
                ("journal", models.CharField(blank=True, default="", max_length=500, verbose_name="Journal")),
                ("year", models.PositiveIntegerField(blank=True, null=True, verbose_name="Year")),
                ("volume_pages", models.CharField(blank=True, default="", max_length=200, verbose_name="Volume / pages")),
                ("doi", models.CharField(blank=True, db_index=True, default="", max_length=200, verbose_name="DOI")),
                ("url", models.CharField(blank=True, default="", help_text="Journal page, PDF, or other link.", max_length=500, verbose_name="URL")),
                ("facility_note", models.TextField(blank=True, default="", help_text="Brief note on how the instrument(s) were used.", verbose_name="Facility use note")),
                ("citation", models.TextField(blank=True, default="", help_text="Assembled or user-provided citation text.", verbose_name="Citation")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")], db_index=True, default="pending", max_length=20, verbose_name="Status")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="Reviewed at")),
                ("rejection_reason", models.TextField(blank=True, default="", verbose_name="Rejection reason")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_publication_claims_reviewed",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Reviewed by",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="equipment_publication_claims",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Submitted by",
                    ),
                ),
                (
                    "equipments",
                    models.ManyToManyField(
                        related_name="publication_claims",
                        to="equipment.equipment",
                        verbose_name="Equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment publication claim",
                "verbose_name_plural": "Equipment publication claims",
                "ordering": ["-created_at", "claim_id"],
            },
        ),
        migrations.AddField(
            model_name="equipmentpublication",
            name="doi",
            field=models.CharField(blank=True, db_index=True, default="", help_text="Normalized DOI when known (used for deduplication).", max_length=200, verbose_name="DOI"),
        ),
        migrations.AddField(
            model_name="equipmentpublication",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="equipment_publications_submitted",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Submitted by",
            ),
        ),
        migrations.AddField(
            model_name="equipmentpublication",
            name="source_claim",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_publications",
                to="equipment.equipmentpublicationclaim",
                verbose_name="Source claim",
            ),
        ),
    ]