from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0140_operatorleaverequest_equipment_nullable"),
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentOperatorCoverage",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("SECONDARY_OPERATOR", "Secondary operator covers"),
                            ("OIC_SELF_OPERATE", "OIC self-operates"),
                            ("OPERATOR_ON_LEAVE", "Operator on leave (disruption policy)"),
                        ],
                        max_length=32,
                    ),
                ),
                ("starts_at", models.DateTimeField(db_index=True)),
                ("ends_at", models.DateTimeField(db_index=True)),
                ("ended_early_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "acting_operator",
                    models.ForeignKey(
                        blank=True,
                        help_text="Operator who will temporarily handle this equipment (for SECONDARY_OPERATOR mode).",
                        limit_choices_to={"user_type": "OPERATOR"},
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="acting_operator_coverages",
                        to="users.user",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="operator_coverages_created",
                        to="users.user",
                    ),
                ),
                (
                    "ended_early_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="operator_coverages_ended",
                        to="users.user",
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="operator_coverages",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "primary_operator",
                    models.ForeignKey(
                        limit_choices_to={"user_type": "OPERATOR"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="primary_operator_coverages",
                        to="users.user",
                    ),
                ),
                (
                    "source_leave_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="operator_coverages",
                        to="equipment.operatorleaverequest",
                    ),
                ),
            ],
            options={
                "ordering": ["-starts_at"],
            },
        ),
        migrations.AddIndex(
            model_name="equipmentoperatorcoverage",
            index=models.Index(fields=["equipment", "starts_at", "ends_at"], name="equip_opcov_equipment_8a9b0a_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentoperatorcoverage",
            index=models.Index(fields=["acting_operator", "starts_at", "ends_at"], name="equip_opcov_acting_o_39f0d6_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentoperatorcoverage",
            index=models.Index(fields=["primary_operator", "starts_at", "ends_at"], name="equip_opcov_primary_o_7c1b6f_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentoperatorcoverage",
            index=models.Index(fields=["mode", "starts_at", "ends_at"], name="equip_opcov_mode_02a0e6_idx"),
        ),
    ]

