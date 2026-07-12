# Email to wallet owner (Supervisor) when a student submits REVIEWER_URGENT urgent booking request.

from django.db import migrations


def add_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(code="urgent_reviewer_pending_supervisor_email").exists():
        return
    CommunicationTemplate.objects.create(
        name="Urgent reviewer request – Supervisor action required",
        code="urgent_reviewer_pending_supervisor_email",
        communication_type="email",
        subject="Action required: Urgent booking request (reviewer evidence) – {{ equipment_name }}",
        body_text="""Hello {{ supervisor_name }},

{{ requester_name }} ({{ requester_email }}) has submitted an urgent booking request of type "Urgent comment from reviewer" with documentary evidence.

Equipment: {{ equipment_name }} ({{ equipment_code }})
Request ID: {{ request_id }}

Please sign in to the IIC Booking portal and approve or reject this request under your Supervisor queue before Admin/OIC can review it.

Open Supervisor urgent requests: {{ link }}

If you did not expect this message, please contact the IIC booking desk.

— IIC Booking""",
        body_html="",
        description="Sent to the wallet owner (Supervisor) when a user submits REVIEWER_URGENT urgent booking request.",
        variable_help=(
            "Variables: {{ supervisor_name }}, {{ requester_name }}, {{ requester_email }}, "
            "{{ equipment_name }}, {{ equipment_code }}, {{ request_id }}, {{ link }}."
        ),
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="urgent_reviewer_pending_supervisor_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0028_update_waitlist_email_body_with_request_time"),
    ]

    operations = [
        migrations.RunPython(add_template, remove_template),
    ]
