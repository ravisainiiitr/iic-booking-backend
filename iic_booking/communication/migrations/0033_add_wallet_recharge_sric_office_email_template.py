# Manual migration: SRIC Office email for faculty wallet recharge (editable in admin)

from django.db import migrations


def create_sric_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="wallet_recharge_sric_office_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="Wallet Recharge — SRIC Office (Faculty)",
        code="wallet_recharge_sric_office_email",
        communication_type="email",
        subject=(
            "Urgent IIC wallet Recharge Request from {{ faculty_display_name }} - "
            "Employee Number [{{ emp_id }}]"
        ),
        body_text="""Urgent IIC wallet recharge request (faculty)

Faculty: {{ faculty_display_name }}
Employee number: {{ emp_id }}
Email: {{ user_email }}

Amount: ₹{{ amount }}
Department: {{ department_name }} ({{ department_code }})
Request ID: {{ request_id }}
Requested: {{ request_date }}

Project: {{ project_name }} ({{ project_code }})
Agency: {{ project_agency }}

The accounts team notification may already have been sent separately. This message is for SRIC Office awareness.

Thank you.""",
        body_html="""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
  <p><strong>Urgent IIC wallet recharge request (faculty)</strong></p>
  <table cellpadding="6" style="border-collapse: collapse;">
    <tr><td><strong>Faculty</strong></td><td>{{ faculty_display_name }}</td></tr>
    <tr><td><strong>Employee number</strong></td><td>{{ emp_id }}</td></tr>
    <tr><td><strong>Email</strong></td><td>{{ user_email }}</td></tr>
    <tr><td><strong>Amount</strong></td><td>₹{{ amount }}</td></tr>
    <tr><td><strong>Department</strong></td><td>{{ department_name }} ({{ department_code }})</td></tr>
    <tr><td><strong>Request ID</strong></td><td>{{ request_id }}</td></tr>
    <tr><td><strong>Requested</strong></td><td>{{ request_date }}</td></tr>
    <tr><td><strong>Project</strong></td><td>{{ project_name }} ({{ project_code }})</td></tr>
    <tr><td><strong>Agency</strong></td><td>{{ project_agency }}</td></tr>
  </table>
  <p style="font-size: 12px; color: #666;">Editable in Admin → Communication templates (code: wallet_recharge_sric_office_email).</p>
</body>
</html>""",
        description="Sent to SRIC Office addresses when a faculty member clicks “Send to SRIC Office” after OTP verification on a wallet recharge request.",
        variable_help=(
            "{{ faculty_name }}, {{ faculty_display_name }}, {{ emp_id }}, {{ user_email }}, "
            "{{ amount }}, {{ request_id }}, {{ request_date }}, {{ department_name }}, {{ department_code }}, "
            "{{ project_name }}, {{ project_code }}, {{ project_agency }}"
        ),
        is_active=True,
    )


def remove_sric_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="wallet_recharge_sric_office_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0032_add_ta_duty_allocation_email"),
    ]

    operations = [
        migrations.RunPython(create_sric_template, remove_sric_template),
    ]
