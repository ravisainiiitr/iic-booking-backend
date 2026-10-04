import django.db.models.deletion
from django.db import migrations, models

SCHEDULE_NAME = "Registration faculty decision timeouts (every 15 min)"

USER_TYPE_CHOICES = [
    ("admin", "Admin"),
    ("dept_admin", "Department Administrator"),
    ("manager", "Officer In Charge"),
    ("operator", "Lab Operator"),
    ("finance", "Accounts In Charge"),
    ("org_admin", "Organization Administrator"),
    ("external_relations", "External Relations Administrator"),
    ("student", "IITR Student"),
    ("individual_student", "Individual Student"),
    ("faculty", "IITR Faculty"),
    ("external", "Educational Institute"),
    ("RND", "Govt R&D Organizations"),
    ("Industry", "Industry"),
    ("startup_incubated_iitr", "IITR Startup"),
    ("external_startup_msme", "External Startup/MSME"),
    ("other", "Other"),
]

EVENT_ACTIONS = [
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
    ("timed_out", "Timed out (no faculty decision)"),
    ("account_removed", "Pending account removal"),
    ("user_notified", "User told about the decision window"),
]


def create_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_15_minutes, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")
    if not PeriodicTask.objects.filter(name=SCHEDULE_NAME).exists():
        PeriodicTask.objects.create(
            name=SCHEDULE_NAME,
            task="users.registration_decision_timeouts",
            interval=every_15_minutes,
            enabled=True,
        )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):
    """Additive: new nullable/defaulted columns, choice labels (no SQL) and one periodic task.

    Existing requests get no decision deadline, so the timeout task never touches them.
    """

    dependencies = [
        ("users", "0128_registration_approvals"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="registrationapproval",
            name="decision_deadline",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text=(
                    "Set each time the request is sent to the faculty. A pending request past this time is treated "
                    "as declined. Empty for requests never sent (they are never timed out)."
                ),
                null=True,
                verbose_name="Faculty must decide by",
            ),
        ),
        migrations.AddField(
            model_name="registrationapprovalpolicy",
            name="decision_window_hours",
            field=models.PositiveSmallIntegerField(
                default=24,
                help_text="Hours the faculty member has to decide after a request is sent; afterwards it is treated as declined.",
                verbose_name="Faculty decision window (hours)",
            ),
        ),
        migrations.AddField(
            model_name="registrationapprovaltoken",
            name="outcome",
            field=models.CharField(
                blank=True,
                choices=[
                    ("approved", "Approved"),
                    ("declined", "Declined"),
                    ("timed_out", "Timed out"),
                    ("closed", "Closed"),
                ],
                default="",
                max_length=16,
                verbose_name="Request outcome",
            ),
        ),
        migrations.AddField(
            model_name="registrationapprovaltoken",
            name="subject_name",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="User name"),
        ),
        migrations.AlterField(
            model_name="registrationapprovaltoken",
            name="approval",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tokens",
                to="users.registrationapproval",
            ),
        ),
        migrations.AlterField(
            model_name="registrationapprovalevent",
            name="action",
            field=models.CharField(choices=EVENT_ACTIONS, db_index=True, max_length=24, verbose_name="Action"),
        ),
        migrations.AlterField(
            model_name="user",
            name="user_type",
            field=models.CharField(
                blank=True,
                choices=USER_TYPE_CHOICES,
                help_text="Type of user in the system",
                max_length=50,
                null=True,
                verbose_name="User Type",
            ),
        ),
        migrations.AlterField(
            model_name="usertypeinactivitytimeout",
            name="user_type",
            field=models.CharField(
                choices=USER_TYPE_CHOICES,
                help_text="User type (e.g. student, faculty, admin). Each type can have its own timeout.",
                max_length=50,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="adminpanelroleconfig",
            name="user_type",
            field=models.CharField(
                choices=USER_TYPE_CHOICES,
                help_text="User type this configuration applies to",
                max_length=50,
            ),
        ),
        migrations.RunPython(create_schedule, remove_schedule),
    ]
