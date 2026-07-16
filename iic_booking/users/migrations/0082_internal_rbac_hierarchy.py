from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_permission_definitions(apps, schema_editor):
    PermissionDefinition = apps.get_model("users", "PermissionDefinition")
    rows = [
        ("users.manage", "Manage users", "Create, update, activate, and map users inside a department."),
        ("equipment.manage", "Manage equipment", "Create and update equipment within a department."),
        ("equipment.request_add", "Request equipment addition", "Submit equipment addition requests for Main Admin approval."),
        ("bookings.manage", "Manage bookings", "Manage departmental booking operations and exceptions."),
        ("wallet.manage", "Manage wallet and billing", "Access financial and wallet actions for the department."),
        ("reports.view", "View reports", "View departmental reports and summaries."),
        ("oic.assign", "Assign OIC", "Assign Officer In Charge users to departmental equipment."),
        ("lab.assign", "Assign Lab In-Charge", "Assign Lab In-Charge users to departmental equipment."),
        ("finance.assign", "Assign Accounts In-Charge", "Assign Accounts In-Charge users inside the department."),
        ("permissions.manage_staff", "Manage subordinate permissions", "Grant or revoke staff permissions within department caps."),
    ]
    for code, name, description in rows:
        PermissionDefinition.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": description},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0081_wallet_student_recharge_and_receipt_file"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="access_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When disabled, departmental admin-panel access is blocked for this internal department.",
                verbose_name="Access enabled",
            ),
        ),
        migrations.CreateModel(
            name="PermissionDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(db_index=True, max_length=100, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Permission definition",
                "verbose_name_plural": "Permission definitions",
                "ordering": ["code"],
            },
        ),
        migrations.CreateModel(
            name="DeptAdminPermissionGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dept_admin_permission_grants", to="users.department")),
                ("dept_admin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dept_admin_permission_grants", to=settings.AUTH_USER_MODEL)),
                ("granted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="granted_dept_admin_permission_caps", to=settings.AUTH_USER_MODEL)),
                ("permission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dept_admin_grants", to="users.permissiondefinition")),
            ],
            options={
                "verbose_name": "Department admin permission grant",
                "verbose_name_plural": "Department admin permission grants",
                "ordering": ["department__name", "dept_admin__email", "permission__code"],
                "unique_together": {("department", "dept_admin", "permission")},
            },
        ),
        migrations.CreateModel(
            name="StaffPermissionGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="staff_permission_grants", to="users.department")),
                ("dept_admin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="staff_permission_caps_granted", to=settings.AUTH_USER_MODEL)),
                ("granted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="granted_staff_permissions", to=settings.AUTH_USER_MODEL)),
                ("permission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="staff_grants", to="users.permissiondefinition")),
                ("staff_user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="staff_permission_grants", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Staff permission grant",
                "verbose_name_plural": "Staff permission grants",
                "ordering": ["department__name", "staff_user__email", "permission__code"],
                "unique_together": {("department", "staff_user", "permission")},
            },
        ),
        migrations.RunPython(seed_permission_definitions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="user_type",
            field=models.CharField(blank=True, choices=[("admin", "Admin"), ("dept_admin", "Department Administrator"), ("manager", "Officer In Charge"), ("operator", "Lab Incharge"), ("finance", "Accounts In Charge"), ("student", "IITR Student"), ("individual_student", "Individual Student"), ("faculty", "IITR Faculty"), ("external", "Educational Institute"), ("RND", "Govt R&D Organizations"), ("Industry", "Industry"), ("startup_incubated_iitr", "Startup Incubated at IIT Roorkee"), ("external_startup_msme", "External Startup/MSME"), ("other", "Other")], help_text="Type of user in the system", max_length=50, null=True, verbose_name="User Type"),
        ),
        migrations.AlterField(
            model_name="usertypeinactivitytimeout",
            name="user_type",
            field=models.CharField(choices=[("admin", "Admin"), ("dept_admin", "Department Administrator"), ("manager", "Officer In Charge"), ("operator", "Lab Incharge"), ("finance", "Accounts In Charge"), ("student", "IITR Student"), ("individual_student", "Individual Student"), ("faculty", "IITR Faculty"), ("external", "Educational Institute"), ("RND", "Govt R&D Organizations"), ("Industry", "Industry"), ("startup_incubated_iitr", "Startup Incubated at IIT Roorkee"), ("external_startup_msme", "External Startup/MSME"), ("other", "Other")], help_text="User type (e.g. student, faculty, admin). Each type can have its own timeout.", max_length=50, unique=True),
        ),
    ]
