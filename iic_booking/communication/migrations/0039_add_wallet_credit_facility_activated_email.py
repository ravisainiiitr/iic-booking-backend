# Faculty: temporary credit line confirmed after OTP (user only; not SRIC)

from django.db import migrations


def create_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(code="wallet_recharge_credit_facility_activated_email").exists():
        return
    CommunicationTemplate.objects.create(
        name="Wallet — credit facility activated (recharge request)",
        code="wallet_recharge_credit_facility_activated_email",
        communication_type="email",
        subject="Wallet recharge — temporary credit facility active (Request #{{ request_id }})",
        body_text="""Hello {{ user_name }},

You accepted the temporary credit facility for your pending wallet recharge. It is now active for the department sub-wallet below. This email is for your records only (SRIC office is not copied). Your recharge request is still pending until accounts credits it via the usual parse process.

Recharge request (same as your submitted request)
- Request ID: {{ request_id }}
- Amount: ₹{{ amount }}
- Department: {{ department_name }}{{ department_code_suffix }}
- Request date: {{ request_date }}
{{ project_lines_plain }}

Credit facility (temporary)
- Overdraft limit: ₹{{ credit_limit_amount }} until {{ credit_window_end_display }} ({{ credit_window_days }} day window from activation).
- When accounts credits this recharge, the facility ends and your balance will reflect the credit.

View request: {{ link }}

Thank you for using IIC Booking System.""",
        body_html="""<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF9800; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .recharge-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .credit-box { background-color: #E8F5E9; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .detail-row { margin: 8px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin:0;">Temporary credit facility active</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>You <strong>accepted</strong> the temporary credit facility for your pending wallet recharge. It is now <strong>active</strong> for the department sub-wallet below. This message is for <strong>your records only</strong> (SRIC office is not copied). Your recharge request remains <strong>pending accounts approval</strong> until it is credited via the usual parse process.</p>

            <div class="recharge-details">
                <h3 style="margin-top:0;">Recharge request details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Amount:</span> ₹{{ amount }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }}{{ department_code_suffix }}</div>
                <div class="detail-row"><span class="label">Request date:</span> {{ request_date }}</div>
                {{ project_lines_html }}
            </div>

            <div class="credit-box">
                <h3 style="margin-top:0;">Credit facility</h3>
                <p style="margin:0 0 10px 0;">You may place equipment bookings using an overdraft of up to <strong>₹{{ credit_limit_amount }}</strong> until <strong>{{ credit_window_end_display }}</strong> ({{ credit_window_days }} day window from activation).</p>
                <p style="margin:0;">When accounts credits this recharge, the facility ends and your balance will update.</p>
            </div>

            <p><a href="{{ link }}">View recharge request</a></p>

            <div class="footer">
                <p>Editable in Admin → Communication templates (code: wallet_recharge_credit_facility_activated_email).</p>
            </div>
        </div>
    </div>
</body>
</html>""",
        description=(
            "Sent only to the faculty user when the temporary wallet credit facility is activated after OTP; "
            "mirrors recharge request copy. Does not notify SRIC office."
        ),
        variable_help=(
            "{{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, "
            "{{ department_name }}, {{ department_code_suffix }}, {{ project_lines_plain }}, {{ project_lines_html }}, "
            "{{ credit_limit_amount }}, {{ credit_window_end_display }}, {{ credit_window_days }}, {{ link }}"
        ),
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="wallet_recharge_credit_facility_activated_email").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0038_update_sric_wallet_email_testing_grant_format"),
    ]

    operations = [
        migrations.RunPython(create_template, remove_template),
    ]
