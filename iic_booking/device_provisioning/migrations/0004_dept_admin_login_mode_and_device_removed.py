# Phase — Department Administrator Login mode + retired device removal audit

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("device_provisioning", "0003_equipment_pc_audit_actions_r24"),
    ]

    operations = [
        migrations.AlterField(
            model_name="departmentprovisioningpolicy",
            name="provisioning_mode",
            field=models.CharField(
                choices=[
                    ("manual_approval", "Manual Approval"),
                    ("trusted_auto_approve", "Trusted Auto-Approve"),
                    ("restricted_auto_approve", "Restricted Auto-Approve"),
                    ("department_administrator_login", "Department Administrator Login"),
                    ("device_code", "Device Code Approval"),
                ],
                db_index=True,
                default="trusted_auto_approve",
                max_length=32,
            ),
        ),
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
                    ("device_removed", "Device Removed"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
