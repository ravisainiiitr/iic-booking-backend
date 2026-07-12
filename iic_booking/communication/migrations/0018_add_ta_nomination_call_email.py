# TA Operating Nomination Call – email to all Internal (Faculty) users

from django.db import migrations


def create_ta_nomination_call_template(apps, schema_editor):
    """Create email template for TA operating nomination call sent to all Faculty."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="ta_operating_nomination_call_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="TA Operating Nomination Call (to Faculty)",
        code="ta_operating_nomination_call_email",
        communication_type="email",
        subject="Call for TA Nominations – {{ instrument_name }} ({{ instrument_code }}) – {{ semester_name }}",
        body_text="""Hello {{ faculty_name }},

You are receiving this as an Internal Faculty member. The following call for TA (Teaching Assistant) nominations for operating equipment has been issued.

Instrument name: {{ instrument_name }}
Instrument code: {{ instrument_code }}
Semester: {{ semester_name }}

Number of operators required: {{ number_of_operators_required }}

Eligibility criteria:
{{ eligibility_criteria }}

Expected duty hours:
{{ expected_duty_hours }}

Benefits:
{{ benefits }}

Nomination deadline: {{ nomination_deadline }}

Please submit your nominations before the deadline through the IIC Booking portal. Only students for whom you are the supervisor can be nominated.

{{ portal_url }}

— IIC Booking""",
        body_html="",
        description="Sent to all Internal (Faculty) users when OIC or Admin initiates a TA nomination call for operating a particular equipment. Contains instrument name, number of operators required, eligibility criteria, expected duty hours, benefits, and nomination deadline.",
        variable_help="Variables: {{ faculty_name }}, {{ faculty_email }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, {{ number_of_operators_required }}, {{ eligibility_criteria }}, {{ expected_duty_hours }}, {{ benefits }}, {{ nomination_deadline }}, {{ portal_url }}.",
        is_active=True,
    )


def remove_ta_nomination_call_template(apps, schema_editor):
    """Remove TA nomination call template (reverse)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="ta_operating_nomination_call_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0017_add_support_ticket_resolution_email"),
    ]

    operations = [
        migrations.RunPython(create_ta_nomination_call_template, remove_ta_nomination_call_template),
    ]
