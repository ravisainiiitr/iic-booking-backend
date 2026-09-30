from django.db import migrations, models

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
    ("startup_incubated_iitr", "Startup Incubated at IIT Roorkee"),
    ("external_startup_msme", "External Startup/MSME"),
    ("other", "Other"),
]

OLD_PERMISSION = ("Assign Lab In-Charge", "Assign Lab In-Charge users to departmental equipment.")
NEW_PERMISSION = ("Assign Lab Operator", "Assign Lab Operator users to departmental equipment.")


def _relabel_lab_assign(apps, old, new):
    PermissionDefinition = apps.get_model("users", "PermissionDefinition")
    PermissionDefinition.objects.filter(code="lab.assign", name=old[0]).update(name=new[0])
    PermissionDefinition.objects.filter(code="lab.assign", description=old[1]).update(description=new[1])


def forwards(apps, schema_editor):
    _relabel_lab_assign(apps, OLD_PERMISSION, NEW_PERMISSION)


def backwards(apps, schema_editor):
    _relabel_lab_assign(apps, NEW_PERMISSION, OLD_PERMISSION)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0118_user_dashboard_menu_layout"),
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
        migrations.RunPython(forwards, backwards),
    ]
