# Generated manually

from django.db import migrations


def fix_user_active_status(apps, schema_editor):
    """Fix is_active status for all users based on email_verified and admin_approved."""
    User = apps.get_model("users", "User")
    
    # Update all users: is_active = email_verified AND admin_approved
    User.objects.filter(email_verified=True, admin_approved=True).update(is_active=True)
    User.objects.exclude(email_verified=True, admin_approved=True).update(is_active=False)


def reverse_fix_user_active_status(apps, schema_editor):
    """Reverse migration - don't change anything."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0013_add_email_verification_and_admin_approval"),
    ]

    operations = [
        migrations.RunPython(fix_user_active_status, reverse_fix_user_active_status),
    ]

