# Generated manually to add Bulk Email template for admin bulk email (Change slot status page).

from django.db import migrations


def create_bulk_email_template(apps, schema_editor):
    """Create email template for admin bulk email to booked slot users."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="admin_bulk_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="Bulk Email (Booked Slots)",
        code="admin_bulk_email",
        communication_type="email",
        subject="Message from IIC Booking",
        body_text="""Hello,

This is a message from the IIC Booking team regarding your equipment booking(s).

You can view your bookings and manage your account in the booking portal.

If you have any questions, please contact the facility.

Thank you for using IIC Booking System.""",
        body_html="",
        description="Default template for sending bulk email to users with booked slots (Change slot status page). Editable in Admin Settings > Communication.",
        variable_help="Optional variables (if supported by sender): {{ user_name }}, {{ user_email }}. This template is used as default content; admin can edit before sending.",
        is_active=True,
    )


def remove_bulk_email_template(apps, schema_editor):
    """Remove bulk email template (reverse migration)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="admin_bulk_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0009_add_booking_reminder_email_template"),
    ]

    operations = [
        migrations.RunPython(create_bulk_email_template, remove_bulk_email_template),
    ]
