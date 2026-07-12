from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0138_equipmentoperator_role"),
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperatorLeaveRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("start_date", models.DateField()),
                ("start_session", models.CharField(choices=[("FN", "Forenoon"), ("AN", "Afternoon")], default="FN", max_length=2)),
                ("end_date", models.DateField()),
                ("end_session", models.CharField(choices=[("FN", "Forenoon"), ("AN", "Afternoon")], default="AN", max_length=2)),
                ("reason", models.TextField()),
                ("attachment", models.FileField(blank=True, null=True, upload_to="operator_leave_attachments/")),
                ("status", models.CharField(choices=[("PENDING", "Pending OIC approval"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"), ("CANCELLED", "Cancelled by operator")], default="PENDING", max_length=16)),
                ("rejection_reason", models.TextField(blank=True, null=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("equipment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operator_leave_requests", to="equipment.equipment")),
                ("operator", models.ForeignKey(limit_choices_to={"user_type": "OPERATOR"}, on_delete=django.db.models.deletion.CASCADE, related_name="operator_leave_requests", to="users.user")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="operator_leave_requests_reviewed", to="users.user")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="operatorleaverequest",
            index=models.Index(fields=["operator", "start_date", "end_date"], name="equip_opera_operator_9b9a6a_idx"),
        ),
        migrations.AddIndex(
            model_name="operatorleaverequest",
            index=models.Index(fields=["equipment", "status", "start_date"], name="equip_opera_equipment_2e7b3a_idx"),
        ),
    ]

