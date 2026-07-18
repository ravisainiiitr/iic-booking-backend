from django.db import migrations


def create_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(
        code="sample_submission_deadline_reminder_email"
    ).exists():
        return

    CommunicationTemplate.objects.create(
        name="Sample Submission Deadline Reminder Email",
        code="sample_submission_deadline_reminder_email",
        communication_type="email",
        subject=(
            "Action needed: Sample submission deadline approaching — "
            "{{ equipment_name }} (Booking #{{ booking_id }})"
        ),
        body_text="""Hello {{ user_name }},

Your sample submission deadline is approaching for the following booking.

Please submit your sample before the deadline so lab processing can proceed on schedule.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Slot start: {{ start_time }}
- Sample submission deadline: {{ submission_deadline }}
- Time remaining: {{ remaining_label }}
- Lead time setting: {{ lead_hours }} hour(s) before slot start
- This notice was sent {{ advance_hours }} hour(s) before the deadline

View your booking: {{ link }}

{{ user_sample_preparation_notice }}
{{ equipment_booking_email_extra }}

Thank you for using IIT Roorkee IIC Booking System!
""",
        body_html="""<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #d97706; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #d97706; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .alert { background-color: #fffbeb; border: 1px solid #fbbf24; padding: 12px; border-radius: 6px; margin: 12px 0; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Sample submission deadline approaching</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <div class="alert">
                Please submit your sample before <strong>{{ submission_deadline }}</strong>
                (about <strong>{{ remaining_label }}</strong> remaining).
                This notice is sent {{ advance_hours }} hour(s) before the submission deadline
                ({{ lead_hours }} hour(s) before slot start).
            </div>
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Slot start:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">Submission deadline:</span> {{ submission_deadline }}</div>
            </div>
            <p>View your booking: <a href="{{ link }}">{{ link }}</a></p>
            <div>{{ user_sample_preparation_notice_html }}</div>
            <div>{{ equipment_booking_email_extra_html }}</div>
            <div class="footer">
                <p>Thank you for using IIT Roorkee IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>""",
        description=(
            "Email sent when sample submission deadline is within 12 hours "
            "(deadline = slot start minus equipment sample_submission_lead_hours)."
        ),
        variable_help=(
            "Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, "
            "{{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, "
            "{{ submission_deadline }}, {{ lead_hours }}, {{ advance_hours }}, "
            "{{ remaining_label }}, {{ link }}"
        ),
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(
        code="sample_submission_deadline_reminder_email"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0046_replace_iic_booking_with_iit_roorkee"),
    ]

    operations = [
        migrations.RunPython(create_template, remove_template),
    ]
