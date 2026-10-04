from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0223_equipment_visible_to_test_accounts_only"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChargeCopyBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_user_type", models.CharField(max_length=50)),
                ("target_user_type", models.CharField(max_length=50)),
                (
                    "created",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="IDs created by this run: charge_profiles, input_fields, param_definitions.",
                    ),
                ),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("rolled_back_at", models.DateTimeField(blank=True, null=True)),
                ("rollback_summary", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "verbose_name": "Charge copy batch",
                "verbose_name_plural": "Charge copy batches",
                "ordering": ["-created_at"],
            },
        ),
    ]
