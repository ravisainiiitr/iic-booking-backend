import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

SCHEDULE_NAME = "Daily registration programme expiry (no-op until enabled)"

CHANNELS = [
    ("portal", "Portal"),
    ("email_link", "Email link"),
    ("login", "Sign-in page"),
    ("system", "System"),
]


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="6",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    if not PeriodicTask.objects.filter(name=SCHEDULE_NAME).exists():
        PeriodicTask.objects.create(
            name=SCHEDULE_NAME,
            task="users.registration_programme_expiry",
            crontab=crontab,
            enabled=True,
        )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0127_wallet_payment_modes"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="RegistrationApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending_faculty", "Pending faculty"),
                            ("pending_admin", "Pending admin"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                        ],
                        db_index=True,
                        default="pending_admin",
                        max_length=20,
                        verbose_name="Status",
                    ),
                ),
                ("forwarded_at", models.DateTimeField(blank=True, null=True, verbose_name="Forwarded at")),
                ("forward_count", models.PositiveIntegerField(default=0, verbose_name="Times forwarded")),
                ("last_reminder_at", models.DateTimeField(blank=True, null=True, verbose_name="Last reminder at")),
                ("reminder_count", models.PositiveIntegerField(default=0, verbose_name="Reminders sent")),
                ("first_viewed_at", models.DateTimeField(blank=True, null=True, verbose_name="First viewed by faculty at")),
                ("decided_at", models.DateTimeField(blank=True, null=True, verbose_name="Decided at")),
                ("decided_role", models.CharField(blank=True, default="", max_length=20, verbose_name="Decided as")),
                ("decision_reason", models.TextField(blank=True, default="", verbose_name="Decision reason")),
                (
                    "decision_channel",
                    models.CharField(blank=True, choices=CHANNELS, default="", max_length=16, verbose_name="Decision channel"),
                ),
                ("disclaimer_text", models.TextField(blank=True, default="", verbose_name="Disclaimer confirmed")),
                ("disclaimer_version", models.CharField(blank=True, default="", max_length=32, verbose_name="Disclaimer version")),
                ("expiry_disabled_at", models.DateTimeField(blank=True, null=True, verbose_name="Disabled at programme expiry")),
                (
                    "expiry_set_force_inactive",
                    models.BooleanField(
                        default=False,
                        help_text="True only when the expiry automation turned on Force Inactive, so an extension can undo it.",
                        verbose_name="Disabled by the expiry automation",
                    ),
                ),
                (
                    "expiry_warnings_sent",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='{"<programme end date>": [30, 7, 1]} — warnings already sent for that end date.',
                        verbose_name="Expiry warnings sent",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Decided by",
                    ),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="registration_approvals_as_faculty",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Faculty the request is addressed to",
                    ),
                ),
                (
                    "forwarded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Forwarded by",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="registration_approval",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Registration approval",
                "verbose_name_plural": "Registration approvals",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["faculty", "status"], name="users_regappr_fac_status")],
            },
        ),
        migrations.CreateModel(
            name="RegistrationExtensionRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("denied", "Denied"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                        verbose_name="Status",
                    ),
                ),
                (
                    "previous_end_date",
                    models.DateField(blank=True, null=True, verbose_name="Programme validity before the extension"),
                ),
                ("max_until", models.DateField(verbose_name="Latest date allowed (6 months)")),
                ("approved_until", models.DateField(blank=True, null=True, verbose_name="Extended until")),
                ("user_reason", models.TextField(blank=True, default="", verbose_name="Reason given by the user")),
                (
                    "requested_channel",
                    models.CharField(blank=True, choices=CHANNELS, default="", max_length=16, verbose_name="Requested from"),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True, verbose_name="Decided at")),
                ("decided_role", models.CharField(blank=True, default="", max_length=20, verbose_name="Decided as")),
                ("decision_reason", models.TextField(blank=True, default="", verbose_name="Decision reason")),
                (
                    "decision_channel",
                    models.CharField(blank=True, choices=CHANNELS, default="", max_length=16, verbose_name="Decision channel"),
                ),
                ("disclaimer_text", models.TextField(blank=True, default="", verbose_name="Disclaimer confirmed")),
                ("disclaimer_version", models.CharField(blank=True, default="", max_length=32, verbose_name="Disclaimer version")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Decided by",
                    ),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="registration_extensions_as_faculty",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Faculty",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="registration_extensions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Programme extension request",
                "verbose_name_plural": "Programme extension requests",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["faculty", "status"], name="users_regext_fac_status")],
            },
        ),
        migrations.CreateModel(
            name="RegistrationApprovalToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True, verbose_name="Token hash")),
                (
                    "purpose",
                    models.CharField(
                        choices=[("registration", "Registration"), ("extension", "Extension")],
                        max_length=16,
                        verbose_name="Purpose",
                    ),
                ),
                ("expires_at", models.DateTimeField(verbose_name="Expires at")),
                ("used_at", models.DateTimeField(blank=True, null=True, verbose_name="Used at")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                (
                    "approval",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tokens",
                        to="users.registrationapproval",
                    ),
                ),
                (
                    "extension",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tokens",
                        to="users.registrationextensionrequest",
                    ),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Faculty",
                    ),
                ),
            ],
            options={
                "verbose_name": "Registration approval link",
                "verbose_name_plural": "Registration approval links",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="RegistrationApprovalEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("subject_email", models.EmailField(blank=True, db_index=True, default="", max_length=254, verbose_name="User email")),
                ("subject_name", models.CharField(blank=True, default="", max_length=255, verbose_name="User name")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("submitted", "Submitted"),
                            ("forwarded", "Forwarded to faculty"),
                            ("reminder_sent", "Reminder sent"),
                            ("viewed", "Viewed by faculty"),
                            ("approved", "Approved"),
                            ("disapproved", "Disapproved"),
                            ("admin_override", "Admin override"),
                            ("faculty_changed", "Faculty changed"),
                            ("expiry_warning", "Expiry warning sent"),
                            ("disabled", "Disabled at programme expiry"),
                            ("extension_requested", "Extension requested"),
                            ("extension_granted", "Extension granted"),
                            ("extension_denied", "Extension denied"),
                            ("re_enabled", "Re-enabled"),
                            ("token_refused", "Review link refused"),
                            ("email_failed", "Email failed"),
                            ("automation_changed", "Expiry automation switched"),
                        ],
                        db_index=True,
                        max_length=24,
                        verbose_name="Action",
                    ),
                ),
                ("actor_email", models.EmailField(blank=True, default="", max_length=254, verbose_name="Actor email")),
                ("actor_role", models.CharField(blank=True, default="", max_length=24, verbose_name="Actor role")),
                (
                    "channel",
                    models.CharField(blank=True, choices=CHANNELS, default="", max_length=16, verbose_name="Channel"),
                ),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP address")),
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
                    "approval",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="users.registrationapproval",
                    ),
                ),
                (
                    "extension",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="users.registrationextensionrequest",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Registration approval event",
                "verbose_name_plural": "Registration approval events",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="RegistrationApprovalPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expiry_automation_enabled", models.BooleanField(default=False, verbose_name="Programme expiry automation enabled")),
                ("warning_days", models.CharField(default="30,7,1", max_length=64, verbose_name="Warning days before expiry")),
                ("token_valid_days", models.PositiveSmallIntegerField(default=14, verbose_name="Faculty review link valid for (days)")),
                ("enabled_at", models.DateTimeField(blank=True, null=True, verbose_name="Enabled at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Updated by",
                    ),
                ),
            ],
            options={
                "verbose_name": "Registration approval policy",
                "verbose_name_plural": "Registration approval policy",
            },
        ),
        migrations.RunPython(create_schedule, remove_schedule),
    ]
