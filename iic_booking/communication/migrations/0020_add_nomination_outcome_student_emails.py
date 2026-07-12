# Student nomination outcome – email to student when nomination is approved or rejected

from django.db import migrations


def create_outcome_templates(apps, schema_editor):
    """Create email templates for notifying student when their nomination is approved or rejected."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="nomination_approved_student_email").exists():
        CommunicationTemplate.objects.create(
            name="Nomination approved (to Student)",
            code="nomination_approved_student_email",
            communication_type="email",
            subject="Your nomination for {{ instrument_name }} has been approved",
            body_text="""Hello {{ student_name }},

Your nomination for operating the following equipment has been approved:

Instrument: {{ instrument_name }} ({{ instrument_code }})
Semester: {{ semester_name }}

You may now proceed as per the guidelines. For any queries, please contact the OIC or Admin.

{{ portal_url }}

— IIC Booking""",
            body_html="",
            description="Sent to the student when OIC/Admin approves their equipment operating nomination.",
            variable_help="Variables: {{ student_name }}, {{ student_email }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, {{ portal_url }}.",
            is_active=True,
        )

    if not CommunicationTemplate.objects.filter(code="nomination_rejected_student_email").exists():
        CommunicationTemplate.objects.create(
            name="Nomination rejected (to Student)",
            code="nomination_rejected_student_email",
            communication_type="email",
            subject="Update on your nomination for {{ instrument_name }} ({{ semester_name }})",
            body_text="""Hello {{ student_name }},

Your nomination for operating the following equipment has not been approved:

Instrument: {{ instrument_name }} ({{ instrument_code }})
Semester: {{ semester_name }}.

Remarks: {{ remarks }}

For further clarification, you may contact the OIC or Admin.

{{ portal_url }}

— IIC Booking""",
            body_html="",
            description="Sent to the student when OIC/Admin rejects their equipment operating nomination. Supports optional {{ remarks }}.",
            variable_help="Variables: {{ student_name }}, {{ student_email }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, {{ remarks }}, {{ portal_url }}.",
            is_active=True,
        )


def remove_outcome_templates(apps, schema_editor):
    """Remove outcome templates (reverse)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="nomination_approved_student_email").delete()
    CommunicationTemplate.objects.filter(code="nomination_rejected_student_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0019_add_student_nomination_intimation_email"),
    ]

    operations = [
        migrations.RunPython(create_outcome_templates, remove_outcome_templates),
    ]
