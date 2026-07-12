# Generated manually to add booking reminder email template (same-day 8:30 AM reminder)

from django.db import migrations


def create_booking_reminder_template(apps, schema_editor):
    """Create email template for same-day booking reminder."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')

    if CommunicationTemplate.objects.filter(code='booking_reminder_email').exists():
        return

    CommunicationTemplate.objects.create(
        name='Booking Reminder Email',
        code='booking_reminder_email',
        communication_type='email',
        subject='Reminder: Your booking is today - {{ equipment_name }} (Booking #{{ booking_id }})',
        body_text='''Hello {{ user_name }},

This is a reminder that you have an equipment booking today.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}
- Duration: {{ total_hours }} hours
- Total Charge: ₹{{ total_charge }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
        body_html='''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #2196F3; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #2196F3; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Reminder: Your booking is today</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>This is a reminder that you have an equipment booking today.</p>
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End Time:</span> {{ end_time }}</div>
                <div class="detail-row"><span class="label">Duration:</span> {{ total_hours }} hours</div>
                <div class="detail-row"><span class="label">Total Charge:</span> ₹{{ total_charge }}</div>
            </div>
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
        description='Email sent as same-day reminder for BOOKED bookings (scheduled daily at 8:30 AM)',
        variable_help='Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ total_charge }}, {{ link }}',
        is_active=True,
    )


def remove_booking_reminder_template(apps, schema_editor):
    """Remove booking reminder email template (reverse migration)."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')
    CommunicationTemplate.objects.filter(code='booking_reminder_email').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0008_add_wallet_recharge_request_email_template'),
    ]

    operations = [
        migrations.RunPython(create_booking_reminder_template, remove_booking_reminder_template),
    ]
