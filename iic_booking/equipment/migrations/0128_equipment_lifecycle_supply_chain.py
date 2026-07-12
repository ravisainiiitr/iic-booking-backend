# Generated manually: lifecycle fields, AMC, expenses, write-off, procurement OIC step

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0127_dailyslot_status_not_available_choice"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="supplier_name",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="Supplier name"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="supplier_contact",
            field=models.TextField(blank=True, default="", verbose_name="Supplier contact"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="purchase_order_ref",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="Purchase order reference"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="purchase_invoice_ref",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="Purchase invoice reference"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="purchase_date",
            field=models.DateField(blank=True, null=True, verbose_name="Purchase date"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="warranty_start",
            field=models.DateField(blank=True, null=True, verbose_name="Warranty start"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="warranty_end",
            field=models.DateField(blank=True, null=True, verbose_name="Warranty end"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="commissioning_date",
            field=models.DateField(blank=True, null=True, verbose_name="Commissioning date"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="asset_serial_number",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="Asset / manufacturer serial"),
        ),
        migrations.AddField(
            model_name="equipment",
            name="lifecycle_notes",
            field=models.TextField(blank=True, default="", verbose_name="Lifecycle notes"),
        ),
        migrations.AddField(
            model_name="equipmentaccessory",
            name="quantity",
            field=models.PositiveIntegerField(default=1, help_text="Quantity supplied with the equipment"),
        ),
        migrations.AddField(
            model_name="equipmentaccessory",
            name="serial_number",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="Serial / tag"),
        ),
        migrations.AddField(
            model_name="equipmentaccessory",
            name="notes",
            field=models.TextField(blank=True, default="", verbose_name="Notes"),
        ),
        migrations.AddField(
            model_name="procurementrequest",
            name="oic_endorsed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="procurementrequest",
            name="oic_endorsed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="procurement_requests_oic_endorsed",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="procurementrequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("SUBMITTED", "Submitted by initiator"),
                    ("PENDING_OIC_REVIEW", "Pending Lab OIC review"),
                    ("UNDER_OFFICE_VERIFICATION", "Under office verification"),
                    ("OFFICE_VERIFIED", "Office verified"),
                    ("PENDING_STORE_APPROVAL", "Pending store approval"),
                    ("STORE_APPROVED", "Store approved"),
                    ("PENDING_HEAD_APPROVAL_EMAIL", "Pending head approval (email)"),
                    ("PENDING_HEAD_APPROVAL_OFFLINE", "Pending head approval (offline)"),
                    ("HEAD_APPROVED", "Head approved"),
                    ("PURCHASE_IN_PROGRESS", "Purchase in progress"),
                    ("PURCHASE_COMPLETED_PENDING_OFFICE_SEEN", "Purchase completed pending office seen"),
                    ("OFFICE_SEEN_COMPLETED", "Office seen and completed"),
                    ("REJECTED_BY_OFFICE", "Rejected by office"),
                    ("REJECTED_BY_STORE", "Rejected by store"),
                    ("REJECTED_BY_HEAD", "Rejected by head"),
                    ("REJECTED_BY_OIC", "Rejected by Lab OIC"),
                    ("CANCELLED", "Cancelled"),
                ],
                default="DRAFT",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="procurementactionlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("SUBMITTED", "Submitted"),
                    ("OIC_ENDORSED", "Lab OIC endorsed"),
                    ("OIC_REJECTED", "Lab OIC rejected"),
                    ("OFFICE_VERIFIED", "Office verified"),
                    ("OFFICE_REJECTED", "Office rejected"),
                    ("STORE_APPROVED", "Store approved"),
                    ("STORE_REJECTED", "Store rejected"),
                    ("HEAD_APPROVED", "Head approved"),
                    ("HEAD_REJECTED", "Head rejected"),
                    ("PURCHASE_MARKED_COMPLETE", "Purchase marked complete"),
                    ("OFFICE_SEEN", "Office seen"),
                ],
                max_length=40,
            ),
        ),
        migrations.CreateModel(
            name="EquipmentAMCContract",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("vendor_name", models.CharField(max_length=255)),
                ("contract_reference", models.CharField(blank=True, default="", max_length=120)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("contract_value", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14)),
                ("coverage_notes", models.TextField(blank=True, default="")),
                ("contract_document", models.FileField(blank=True, null=True, upload_to="equipment_amc/%Y/%m/%d/")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="amc_contracts_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="amc_contracts",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "ordering": ["-start_date"],
            },
        ),
        migrations.CreateModel(
            name="EquipmentWriteOffRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("request_no", models.CharField(blank=True, editable=False, max_length=100, unique=True)),
                ("reason", models.TextField()),
                ("asset_classification", models.CharField(blank=True, default="", max_length=20)),
                ("estimated_residual_value", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_OFFICE", "Pending Office Superintendent"),
                            ("PENDING_STORE", "Pending Store In Charge"),
                            ("PENDING_HEAD", "Pending Head of Department"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                            ("EXECUTED", "Executed (asset written off)"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PENDING_OFFICE",
                        max_length=30,
                    ),
                ),
                ("office_reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("store_reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("head_reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("executed_at", models.DateTimeField(blank=True, null=True)),
                ("rejection_comments", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="write_off_requests",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "executed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="write_off_executed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "head_reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="write_off_head_reviewed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "initiated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="write_off_requests_initiated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "office_reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="write_off_office_reviewed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "store_reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="write_off_store_reviewed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="EquipmentExpense",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "expense_type",
                    models.CharField(
                        choices=[
                            ("AMC", "AMC / service contract payment"),
                            ("CALIBRATION", "Calibration"),
                            ("REPAIR", "Repair"),
                            ("CONSUMABLE", "Consumable (direct)"),
                            ("PROCUREMENT_LINKED", "Linked to procurement request"),
                            ("OTHER", "Other"),
                        ],
                        default="OTHER",
                        max_length=30,
                    ),
                ),
                ("classification", models.CharField(blank=True, default="", help_text="Consumable / minor / major — optional tag", max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("expense_date", models.DateField()),
                ("description", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "amc_contract",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="expense_entries",
                        to="equipment.equipmentamccontract",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_expenses_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="equipment_expenses",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "procurement_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="linked_expenses",
                        to="equipment.procurementrequest",
                    ),
                ),
            ],
            options={
                "ordering": ["-expense_date", "-id"],
            },
        ),
        migrations.CreateModel(
            name="EquipmentWriteOffActionLog",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("SUBMITTED", "Submitted"),
                            ("OFFICE_FORWARDED", "Office forwarded"),
                            ("OFFICE_REJECTED", "Office rejected"),
                            ("STORE_FORWARDED", "Store forwarded"),
                            ("STORE_REJECTED", "Store rejected"),
                            ("HEAD_APPROVED", "Head approved"),
                            ("HEAD_REJECTED", "Head rejected"),
                            ("EXECUTED", "Marked executed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        max_length=40,
                    ),
                ),
                ("comments", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "by_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="write_off_actions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_logs",
                        to="equipment.equipmentwriteoffrequest",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="equipmentamccontract",
            index=models.Index(fields=["equipment", "is_active", "end_date"], name="equipment_e_equipme_d3e0b8_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentexpense",
            index=models.Index(fields=["equipment", "expense_date"], name="equipment_e_equipme_7a8c2f_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentwriteoffrequest",
            index=models.Index(fields=["equipment", "status", "created_at"], name="equipment_e_equipme_9f1a2c_idx"),
        ),
        migrations.AddIndex(
            model_name="equipmentwriteoffrequest",
            index=models.Index(fields=["initiated_by", "status"], name="equipment_e_initiat_4b5d6e_idx"),
        ),
    ]
