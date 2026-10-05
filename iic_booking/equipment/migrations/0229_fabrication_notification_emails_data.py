from django.db import migrations


def copy_single_email_into_list(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    for eq in Equipment.objects.exclude(print_3d_stl_notification_email="").only(
        "pk", "print_3d_stl_notification_email", "fabrication_notification_emails"
    ):
        email = (eq.print_3d_stl_notification_email or "").strip()
        existing = [e for e in (eq.fabrication_notification_emails or []) if isinstance(e, str)]
        if email and email.lower() not in {e.lower() for e in existing}:
            eq.fabrication_notification_emails = [email, *existing]
            eq.save(update_fields=["fabrication_notification_emails"])


def copy_first_list_email_back(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    for eq in Equipment.objects.only("pk", "print_3d_stl_notification_email", "fabrication_notification_emails"):
        emails = [e for e in (eq.fabrication_notification_emails or []) if isinstance(e, str) and e.strip()]
        if emails and not (eq.print_3d_stl_notification_email or "").strip():
            eq.print_3d_stl_notification_email = emails[0].strip()
            eq.save(update_fields=["print_3d_stl_notification_email"])


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0228_fabrication_laser_cut_profile"),
    ]

    operations = [
        migrations.RunPython(copy_single_email_into_list, copy_first_list_email_back),
    ]
