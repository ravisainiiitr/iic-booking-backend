# Editable template: faculty credit window ended without parse credit

from django.db import migrations


def create_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(code="wallet_credit_facility_expired_email").exists():
        return
    CommunicationTemplate.objects.create(
        name="Wallet — credit facility expired (bookings on hold)",
        code="wallet_credit_facility_expired_email",
        communication_type="email",
        subject="[IIC] Wallet recharge credit window ended — bookings on hold (Request #{{ request_id }})",
        body_text="""Dear {{ user_name }},

The temporary credit facility linked to wallet recharge request #{{ request_id }} ({{ department_name }}) has ended because the recharge was not credited via the accounts (parse) process within the allowed window.

Equipment bookings for that department sub-wallet are on hold until the recharge is realized. Your balance may show as negative if you used the credit line.

If the recharge is credited later, your wallet will be updated and bookings can resume when the balance allows.

Open your wallet: {{ link }}

— IIC Booking System""",
        body_html="""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
  <p>Dear {{ user_name }},</p>
  <p>The temporary <strong>credit facility</strong> linked to wallet recharge request
  <strong>#{{ request_id }}</strong> (<strong>{{ department_name }}</strong>, ₹{{ amount }}) has ended because
  the recharge was not credited via the accounts <strong>(parse)</strong> process within the allowed window.</p>
  <p>Bookings for that <strong>department sub-wallet</strong> are <strong>on hold</strong> until the recharge is realized.
  Your balance may appear negative if you used the credit line.</p>
  <p>Once the recharge is credited (even after the window), your wallet updates and bookings can resume when the balance allows.</p>
  <p><a href="{{ link }}">Open Wallet</a></p>
  <p style="font-size: 12px; color: #666;">Editable in Admin → Communication templates (code: wallet_credit_facility_expired_email).</p>
</body>
</html>""",
        description=(
            "Sent to the faculty wallet owner when a recharge credit window expires without parse credit; "
            "department bookings are blocked until the recharge is credited."
        ),
        variable_help="{{ user_name }}, {{ user_email }}, {{ request_id }}, {{ department_name }}, {{ amount }}, {{ link }}",
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="wallet_credit_facility_expired_email").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0035_update_oic_monthly_report_template"),
    ]

    operations = [
        migrations.RunPython(create_template, remove_template),
    ]
