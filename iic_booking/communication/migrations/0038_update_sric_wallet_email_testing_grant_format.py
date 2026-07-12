# SRIC wallet email: Testing Grant title, colon labels, grant code at end, no admin footer

from django.db import migrations
from django.utils import timezone

_CODE = "wallet_recharge_sric_office_email"
_COMM_TYPE = "email"

_NEW_SUBJECT = (
    "Urgent IIC Testing Grant ({{ grant_code_for_credit }}) credit request — "
    "{{ faculty_display_name }} [{{ emp_id }}]"
)

_NEW_BODY_TEXT = """Urgent IIC Testing Grant ({{ grant_code_for_credit }}) credit request

Faculty Name: {{ faculty_display_name }}
Employee number: {{ emp_id }}
Email: {{ user_email }}
Amount: ₹{{ amount }}
Department: {{ department_name }} ({{ department_code }})
Project Name: {{ project_name }} ({{ project_code }})
Agency: {{ project_agency }}
Requested at: {{ request_date_display }}

Grant code for Credit: {{ grant_code_for_credit }}

The accounts team notification may already have been sent separately. This message is for SRIC Office awareness.

Thank you."""

_NEW_BODY_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
  <p><strong>Urgent IIC Testing Grant ({{ grant_code_for_credit }}) credit request</strong></p>
  <table cellpadding="6" style="border-collapse: collapse;">
    <tr><td><strong>Faculty Name:</strong></td><td>{{ faculty_display_name }}</td></tr>
    <tr><td><strong>Employee number:</strong></td><td>{{ emp_id }}</td></tr>
    <tr><td><strong>Email:</strong></td><td>{{ user_email }}</td></tr>
    <tr><td><strong>Amount:</strong></td><td>₹{{ amount }}</td></tr>
    <tr><td><strong>Department:</strong></td><td>{{ department_name }} ({{ department_code }})</td></tr>
    <tr><td><strong>Project Name:</strong></td><td>{{ project_name }} ({{ project_code }})</td></tr>
    <tr><td><strong>Agency:</strong></td><td>{{ project_agency }}</td></tr>
    <tr><td><strong>Requested at:</strong></td><td>{{ request_date_display }}</td></tr>
    <tr><td><strong>Grant code for Credit:</strong></td><td>{{ grant_code_for_credit }}</td></tr>
  </table>
</body>
</html>"""

_NEW_VARIABLE_HELP = (
    "{{ faculty_name }}, {{ faculty_display_name }}, {{ grant_code_for_credit }}, {{ emp_id }}, {{ user_email }}, "
    "{{ amount }}, {{ request_id }}, {{ request_date }}, {{ request_date_display }}, {{ department_name }}, "
    "{{ department_code }}, {{ project_name }}, {{ project_code }}, {{ project_agency }}, {{ approve_url }}, "
    "{{ reject_url }}"
)


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code=_CODE, communication_type=_COMM_TYPE).update(
        subject=_NEW_SUBJECT,
        body_text=_NEW_BODY_TEXT,
        body_html=_NEW_BODY_HTML,
        variable_help=_NEW_VARIABLE_HELP,
        updated_at=timezone.now(),
    )


def backwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    prev_subject = (
        "Urgent IIC wallet Recharge Request from {{ faculty_display_name }} - "
        "Employee Number [{{ emp_id }}]"
    )
    prev_text = """Urgent IIC wallet recharge request (faculty)

Faculty Name: {{ faculty_display_name }}
Grant Code for Credit: {{ grant_code_for_credit }}
Employee number: {{ emp_id }}
Email: {{ user_email }}

Amount: ₹{{ amount }}
Department: {{ department_name }} ({{ department_code }})
Request ID: {{ request_id }}
Requested Date and Time: {{ request_date_display }}

Project Name: {{ project_name }} ({{ project_code }})
Agency: {{ project_agency }}

The accounts team notification may already have been sent separately. This message is for SRIC Office awareness.

Thank you."""
    prev_html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
  <p><strong>Urgent IIC wallet recharge request (faculty)</strong></p>
  <table cellpadding="6" style="border-collapse: collapse;">
    <tr><td><strong>Faculty Name:</strong></td><td>{{ faculty_display_name }}</td></tr>
    <tr><td><strong>Grant Code for Credit:</strong></td><td>{{ grant_code_for_credit }}</td></tr>
    <tr><td><strong>Employee number</strong></td><td>{{ emp_id }}</td></tr>
    <tr><td><strong>Email</strong></td><td>{{ user_email }}</td></tr>
    <tr><td><strong>Amount</strong></td><td>₹{{ amount }}</td></tr>
    <tr><td><strong>Department</strong></td><td>{{ department_name }} ({{ department_code }})</td></tr>
    <tr><td><strong>Request ID</strong></td><td>{{ request_id }}</td></tr>
    <tr><td><strong>Requested Date and Time:</strong></td><td>{{ request_date_display }}</td></tr>
    <tr><td><strong>Project Name:</strong></td><td>{{ project_name }} ({{ project_code }})</td></tr>
    <tr><td><strong>Agency</strong></td><td>{{ project_agency }}</td></tr>
  </table>
</body>
</html>"""
    prev_help = (
        "{{ faculty_name }}, {{ faculty_display_name }}, {{ grant_code_for_credit }}, {{ emp_id }}, {{ user_email }}, "
        "{{ amount }}, {{ request_id }}, {{ request_date }}, {{ request_date_display }}, {{ department_name }}, "
        "{{ department_code }}, {{ project_name }}, {{ project_code }}, {{ project_agency }}, {{ approve_url }}, "
        "{{ reject_url }}"
    )
    CommunicationTemplate.objects.filter(code=_CODE, communication_type=_COMM_TYPE).update(
        subject=prev_subject,
        body_text=prev_text,
        body_html=prev_html,
        variable_help=prev_help,
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0037_update_wallet_recharge_sric_office_email_template"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
