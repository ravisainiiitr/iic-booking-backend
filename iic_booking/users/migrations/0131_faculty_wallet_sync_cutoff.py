import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Main Administrator setting for the faculty login wallet sync deadline, plus its audit table.

    Schema only: the stored deadline starts empty (built-in default applies) and is set
    afterwards through the audited service so every change has an actor and a reason.
    """

    dependencies = [
        ("users", "0130_procurement_user_types"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="portalmigrationstate",
            name="faculty_wallet_sync_cutoff",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="FacultyWalletSyncCutoffChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("old_cutoff", models.DateTimeField(blank=True, null=True)),
                ("new_cutoff", models.DateTimeField()),
                ("actor_email", models.CharField(blank=True, default="", max_length=255)),
                ("reason", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faculty_wallet_sync_cutoff_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Faculty wallet sync deadline change",
                "verbose_name_plural": "Faculty wallet sync deadline changes",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
