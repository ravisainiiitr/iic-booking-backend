from django.db import migrations, models

USER_TYPE_CHOICES = [
    ("admin", "Admin"),
    ("dept_admin", "Department Administrator"),
    ("manager", "Officer In Charge"),
    ("operator", "Lab Operator"),
    ("finance", "Accounts In Charge"),
    ("org_admin", "Organization Administrator"),
    ("external_relations", "External Relations Administrator"),
    ("oc_stores", "Officer In Charge Stores"),
    ("hod", "Head of Department"),
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


class Migration(migrations.Migration):
    """Adds the Officer In Charge Stores and Head of Department user types (choices only; no data change)."""

    dependencies = [
        ("users", "0129_registration_decision_window"),
    ]

    operations = [
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
    ]
