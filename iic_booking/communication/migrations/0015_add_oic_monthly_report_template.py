# Add OIC monthly report email template (sent with PDF attachment on 1st of month)

from django.db import migrations


def create_oic_monthly_report_template(apps, schema_editor):
    """Create email template for OIC monthly equipment report (with PDF attachment)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="oic_monthly_report").exists():
        CommunicationTemplate.objects.create(
            name="OIC Monthly Equipment Report",
            code="oic_monthly_report",
            communication_type="email",
            subject="Equipment Utilization Report – {{ date_from }} to {{ date_to }}",
            body_text="""Hello,

Please find attached the equipment utilization report for the period {{ date_from }} to {{ date_to }} for the equipment you are in charge of ({{ equipment_codes }}).

The report includes per-equipment booking counts, completed bookings, under maintenance and operator absent hours, booking not utilized, and slots with no booking. A pie chart of overall utilization is included.

— IIC Booking""",
            body_html="",
            description="Sent to each OIC (Officer in Charge) on the 1st of every month with PDF report for their equipment. Schedule: equipment.send_oic_monthly_reports. Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ date_from }}, {{ date_to }}, {{ equipment_codes }}.",
            is_active=True,
        )


def remove_oic_monthly_report_template(apps, schema_editor):
    """Remove OIC monthly report template (reverse migration)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="oic_monthly_report").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0014_update_booking_charge_recalculated_email_body"),
    ]

    operations = [
        migrations.RunPython(create_oic_monthly_report_template, remove_oic_monthly_report_template),
    ]
