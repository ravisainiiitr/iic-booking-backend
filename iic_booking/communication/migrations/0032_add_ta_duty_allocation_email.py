# Email to the assigned TA when Admin/OIC allocates TA duty to a booking.

from django.db import migrations


def create_ta_duty_allocation_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="ta_duty_allocation_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="TA Duty Allocation (to assigned TA)",
        code="ta_duty_allocation_email",
        communication_type="email",
        subject="TA duty assigned – {{ instrument_name }} ({{ instrument_code }}) – booking {{ booking_id }}",
        body_text="""Hello {{ student_name }},

You have been assigned TA (Teaching Assistant) operating duty for the following booking.

Assignment ID: {{ assignment_id }}

Equipment: {{ instrument_name }} ({{ instrument_code }})
Academic period: {{ academic_year_name }}

Booking reference: {{ booking_id }}
Booking date: {{ booking_date }}
Slot start: {{ booking_start }}
Slot end: {{ booking_end }}

Expected duty hours: {{ expected_hours }}

Notes from allocator:
{{ allocation_notes }}

Allocated by: {{ allocated_by_name }}

Please sign in to the booking portal to review and accept or decline this assignment:
{{ portal_url }}

— IIC Booking""",
        body_html="",
        description=(
            "Sent to the TA student when an Admin or OIC allocates TA duty to an approved nomination "
            "and a specific booking."
        ),
        variable_help=(
            "Variables: {{ student_name }}, {{ student_email }}, {{ instrument_name }}, {{ instrument_code }}, "
            "{{ academic_year_name }}, {{ semester_name }}, {{ assignment_id }}, {{ booking_id }}, "
            "{{ booking_date }}, {{ booking_start }}, {{ booking_end }}, {{ expected_hours }}, "
            "{{ allocation_notes }}, {{ portal_url }}, {{ allocated_by_name }}."
        ),
        is_active=True,
    )


def remove_ta_duty_allocation_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="ta_duty_allocation_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0031_repeat_sample_and_urgent_email_templates"),
    ]

    operations = [
        migrations.RunPython(create_ta_duty_allocation_template, remove_ta_duty_allocation_template),
    ]
