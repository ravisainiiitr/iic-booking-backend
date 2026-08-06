# Phase R.2.4 — Equipment PC audit actions

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("device_provisioning", "0002_department_provisioning_policy_r23"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deviceauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("provisioned", "Provisioned"),
                    ("reprovisioned", "Reprovisioned"),
                    ("suspended", "Suspended"),
                    ("revoked", "Revoked"),
                    ("retired", "Retired"),
                    ("renamed", "Renamed"),
                    ("assigned", "Assigned"),
                    ("heartbeat", "Heartbeat"),
                    ("policy_updated", "Policy Updated"),
                    ("provisioning_started", "Provisioning Started"),
                    ("auto_approved", "Auto Approved"),
                    ("auto_approve_denied", "Auto Approve Denied"),
                    ("policy_used", "Policy Used"),
                    ("equipment_selected", "Equipment Selected"),
                    ("equipment_assigned", "Equipment Assigned"),
                    ("provision_completed", "Provision Completed"),
                    ("provision_failed", "Provision Failed"),
                    ("device_replaced", "Device Replaced"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
