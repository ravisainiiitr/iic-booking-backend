import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0124_walletjoinrequest_spending_limits"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupervisorInvite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(db_index=True, max_length=254, verbose_name="Supervisor email")),
                ("supervisor_name", models.CharField(blank=True, default="", max_length=255, verbose_name="Supervisor name")),
                ("message", models.TextField(blank=True, default="", verbose_name="Message")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("accepted", "Accepted"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                        verbose_name="Status",
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True, verbose_name="Token hash")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("expires_at", models.DateTimeField(db_index=True, verbose_name="Expires at")),
                ("last_sent_at", models.DateTimeField(blank=True, null=True, verbose_name="Last sent at")),
                ("send_count", models.PositiveIntegerField(default=0, verbose_name="Times sent")),
                ("accepted_at", models.DateTimeField(blank=True, null=True, verbose_name="Accepted at")),
                ("cancelled_at", models.DateTimeField(blank=True, null=True, verbose_name="Cancelled at")),
                (
                    "accepted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="supervisor_invites_accepted",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Accepted by",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="supervisor_invites",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
                (
                    "join_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="supervisor_invites",
                        to="users.walletjoinrequest",
                        verbose_name="Wallet link request",
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="supervisor_invites_sent",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Student",
                    ),
                ),
            ],
            options={
                "verbose_name": "Supervisor invite",
                "verbose_name_plural": "Supervisor invites",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["email", "status"], name="users_supinv_email_status"),
                    models.Index(fields=["student", "status"], name="users_supinv_student_status"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SupervisorInviteEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("resent", "Resent"),
                            ("cancelled", "Cancelled"),
                            ("expired", "Expired"),
                            ("accepted", "Accepted"),
                            ("refused", "Refused"),
                            ("email_failed", "Email failed"),
                        ],
                        db_index=True,
                        max_length=16,
                        verbose_name="Action",
                    ),
                ),
                ("email", models.EmailField(blank=True, db_index=True, default="", max_length=254, verbose_name="Supervisor email")),
                ("details", models.JSONField(blank=True, default=dict, verbose_name="Details")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Actor",
                    ),
                ),
                (
                    "invite",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="users.supervisorinvite",
                        verbose_name="Invite",
                    ),
                ),
            ],
            options={
                "verbose_name": "Supervisor invite event",
                "verbose_name_plural": "Supervisor invite events",
                "ordering": ["-created_at"],
            },
        ),
    ]
