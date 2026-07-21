# Generated manually for department faculty credit facility

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0090_wallet_peer_transfer"),
    ]

    operations = [
        migrations.CreateModel(
            name="DepartmentFacultyCreditFacilitySettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "enabled",
                    models.BooleanField(
                        default=False,
                        help_text="When disabled, joining-date and credit-limit settings have no effect.",
                        verbose_name="Credit Facility Enabled",
                    ),
                ),
                (
                    "joining_date_cutoff",
                    models.DateField(
                        blank=True,
                        help_text="Faculty are eligible only if their Date of Joining is on or after this date.",
                        null=True,
                        verbose_name="Eligible Date of Joining (on or after)",
                    ),
                ),
                (
                    "max_credit_limit",
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        help_text="Maximum controlled negative balance allowed on the department sub-wallet.",
                        max_digits=10,
                        verbose_name="Maximum Credit Limit (₹)",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated At")),
                (
                    "department",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faculty_credit_facility_settings",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="department_faculty_credit_settings_updates",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Updated By",
                    ),
                ),
            ],
            options={
                "verbose_name": "Department faculty credit facility settings",
                "verbose_name_plural": "Department faculty credit facility settings",
                "db_table": "users_departmentfacultycreditfacilitysettings",
            },
        ),
        migrations.CreateModel(
            name="FacultyDepartmentCreditFacility",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("exhausted", "Exhausted"),
                            ("closed", "Closed"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=20,
                        verbose_name="Status",
                    ),
                ),
                (
                    "credit_limit",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Snapshot of the department max credit limit at activation.",
                        max_digits=10,
                        verbose_name="Credit Limit (₹)",
                    ),
                ),
                ("availed_at", models.DateTimeField(db_index=True, verbose_name="Date Availed")),
                ("closed_at", models.DateTimeField(blank=True, null=True, verbose_name="Date Closed")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated At")),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faculty_credit_facilities",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="department_faculty_credit_facilities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Faculty",
                    ),
                ),
            ],
            options={
                "verbose_name": "Faculty department credit facility",
                "verbose_name_plural": "Faculty department credit facilities",
                "db_table": "users_facultydepartmentcreditfacility",
            },
        ),
        migrations.CreateModel(
            name="FacultyDepartmentCreditFacilityAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("config_updated", "Configuration updated"),
                            ("activated", "Facility activated"),
                            ("outstanding_changed", "Outstanding credit changed"),
                            ("recharge_recovery", "Recharge recovery"),
                            ("status_changed", "Status changed"),
                            ("closed", "Facility closed"),
                        ],
                        db_index=True,
                        max_length=40,
                        verbose_name="Event Type",
                    ),
                ),
                ("message", models.TextField(blank=True, default="", verbose_name="Message")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="Metadata")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created At")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faculty_credit_facility_audit_as_actor",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Actor",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faculty_credit_facility_audit_logs",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
                (
                    "facility",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_logs",
                        to="users.facultydepartmentcreditfacility",
                        verbose_name="Facility",
                    ),
                ),
                (
                    "faculty_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faculty_credit_facility_audit_as_faculty",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Faculty",
                    ),
                ),
            ],
            options={
                "verbose_name": "Faculty department credit facility audit log",
                "verbose_name_plural": "Faculty department credit facility audit logs",
                "db_table": "users_facultydepartmentcreditfacilityauditlog",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="facultydepartmentcreditfacility",
            constraint=models.UniqueConstraint(
                fields=("user", "department"),
                name="uniq_faculty_department_credit_facility",
            ),
        ),
        migrations.AddIndex(
            model_name="facultydepartmentcreditfacility",
            index=models.Index(fields=["department", "status"], name="fdcf_dept_status_idx"),
        ),
    ]
