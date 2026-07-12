# Student nomination intimation – email to student when faculty nominates them

from django.db import migrations


def create_student_nomination_intimation_template(apps, schema_editor):
    """Create email template for notifying student when they are nominated for equipment operation."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="student_nomination_intimation_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="Student nomination intimation (to Student)",
        code="student_nomination_intimation_email",
        communication_type="email",
        subject="You have been nominated for equipment operation – {{ instrument_name }} ({{ semester_name }})",
        body_text="""Hello {{ student_name }},

Your supervisor {{ supervisor_name }} has nominated you for operating the following equipment:

Instrument: {{ instrument_name }} ({{ instrument_code }})
Semester: {{ semester_name }}

Please log in to the IIC Booking portal and submit your resume for this nomination so that the OIC or Admin can review it for further action.

Go to: {{ nomination_requests_url }}

— IIC Booking""",
        body_html="",
        description="Sent to the student when a faculty member nominates them for equipment operation (TA/operating). Asks the student to submit their resume via the portal.",
        variable_help="Variables: {{ student_name }}, {{ student_email }}, {{ instrument_name }}, {{ instrument_code }}, {{ semester_name }}, {{ supervisor_name }}, {{ nomination_requests_url }}.",
        is_active=True,
    )


def remove_student_nomination_intimation_template(apps, schema_editor):
    """Remove student nomination intimation template (reverse)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="student_nomination_intimation_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0018_add_ta_nomination_call_email"),
    ]

    operations = [
        migrations.RunPython(create_student_nomination_intimation_template, remove_student_nomination_intimation_template),
    ]
