# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0003_add_wallet_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="Department",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="Name of the department",
                        max_length=255,
                        unique=True,
                        verbose_name="Department Name",
                    ),
                ),
                (
                    "code",
                    models.CharField(
                        blank=True,
                        help_text="Short code for the department",
                        max_length=50,
                        null=True,
                        unique=True,
                        verbose_name="Department Code",
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Optional description of the department",
                        verbose_name="Description",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Updated at"),
                ),
            ],
            options={
                "verbose_name": "Department",
                "verbose_name_plural": "Departments",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="user",
            name="department",
            field=models.ForeignKey(
                blank=True,
                help_text="Department the user belongs to",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="users",
                to="users.department",
                verbose_name="Department",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="emp_id",
            field=models.CharField(
                blank=True,
                help_text="Employee/Student ID",
                max_length=50,
                null=True,
                unique=True,
                verbose_name="Employee ID",
            ),
        ),
        migrations.CreateModel(
            name="Supervisor",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Updated at"),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        help_text="Faculty member supervising this student",
                        limit_choices_to={"user_type": "faculty"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supervised_students",
                        to="users.user",
                        verbose_name="Faculty Supervisor",
                    ),
                ),
                (
                    "student",
                    models.OneToOneField(
                        help_text="Student who has this supervisor",
                        limit_choices_to={"user_type": "student"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="supervisor_as_student",
                        to="users.user",
                        verbose_name="Student",
                    ),
                ),
            ],
            options={
                "verbose_name": "Supervisor",
                "verbose_name_plural": "Supervisors",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="supervisor",
            constraint=models.UniqueConstraint(
                fields=("student",), name="unique_student_supervisor"
            ),
        ),
    ]

