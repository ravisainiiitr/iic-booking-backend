# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_add_profile_picture"),
    ]

    operations = [
        migrations.CreateModel(
            name="Lab",
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
                        help_text="Name of the lab",
                        max_length=255,
                        verbose_name="Lab Name",
                    ),
                ),
                (
                    "code",
                    models.CharField(
                        blank=True,
                        help_text="Short code for the lab",
                        max_length=50,
                        null=True,
                        unique=True,
                        verbose_name="Lab Code",
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Optional description of the lab",
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
                (
                    "department",
                    models.ForeignKey(
                        help_text="Department this lab belongs to",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="labs",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
            ],
            options={
                "verbose_name": "Lab",
                "verbose_name_plural": "Labs",
                "ordering": ["name"],
            },
        ),
        migrations.AddConstraint(
            model_name="lab",
            constraint=models.UniqueConstraint(
                fields=("name", "department"), name="unique_lab_name_per_department"
            ),
        ),
    ]

