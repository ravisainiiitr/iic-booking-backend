# Registration self-verification email (sent when user clicks Create account)
# and admin approval confirmation email (sent when admin approves user)

from django.db import migrations


def create_registration_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="registration_self_verification_email").exists():
        CommunicationTemplate.objects.create(
            name="Registration self-verification email",
            code="registration_self_verification_email",
            communication_type="email",
            subject="Verify your IIC Booking registration",
            body_text="""Hello {{ name }},

Thank you for registering with IIC Booking.

To complete your registration, please verify your email by clicking the link below:

{{ verification_url }}

If you did not request this registration, you can safely ignore this email.

— IIC Booking""",
            body_html="""<p>Hello {{ name }},</p>
<p>Thank you for registering with IIC Booking.</p>
<p>To complete your registration, please verify your email by clicking the link below:</p>
<p><a href="{{ verification_url }}" style="display:inline-block;background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;">Verify my email</a></p>
<p>Or copy this link into your browser:</p>
<p style="word-break:break-all;color:#666;">{{ verification_url }}</p>
<p>If you did not request this registration, you can safely ignore this email.</p>
<p>— IIC Booking</p>""",
            description="Sent when user clicks Create account (self-verify path). User clicks link to accept or reject registration. No admin approval needed for this path.",
            variable_help="Variables: {{ name }}, {{ verification_url }}.",
            is_active=True,
        )

    if not CommunicationTemplate.objects.filter(code="registration_verification_otp_email").exists():
        CommunicationTemplate.objects.create(
            name="Registration verification OTP email",
            code="registration_verification_otp_email",
            communication_type="email",
            subject="Your IIC Booking registration OTP",
            body_text="""Hello {{ name }},

Your one-time password (OTP) to confirm your registration is: {{ otp }}

Enter this OTP on the registration page to submit your account for admin approval. This OTP expires in 10 minutes. Do not share it with anyone.

— IIC Booking""",
            body_html="""<p>Hello {{ name }},</p>
<p>Your one-time password (OTP) to confirm your registration is:</p>
<p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{{ otp }}</p>
<p>Enter this OTP on the registration page to submit your account for admin approval.</p>
<p>This OTP expires in 10 minutes. Do not share it with anyone.</p>
<p>— IIC Booking</p>""",
            description="Sent when user clicks Create account and admin approval is required (e.g. public email). User enters OTP inline to complete verification; then request goes to admin.",
            variable_help="Variables: {{ name }}, {{ otp }}.",
            is_active=True,
        )

    if not CommunicationTemplate.objects.filter(code="registration_approval_confirmation_email").exists():
        CommunicationTemplate.objects.create(
            name="Registration approval confirmation email",
            code="registration_approval_confirmation_email",
            communication_type="email",
            subject="Your IIC Booking account is approved",
            body_text="""Hello {{ name }},

Your IIC Booking account has been approved. You can now log in and use the online booking facility.

Web address: {{ web_address }}

Log in with the email and password you used during registration.

— IIC Booking""",
            body_html="""<p>Hello {{ name }},</p>
<p>Your IIC Booking account has been approved. You can now log in and use the online booking facility.</p>
<p><a href="{{ web_address }}" style="display:inline-block;background:#2563eb;color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;">Go to IIC Booking</a></p>
<p>Web address: {{ web_address }}</p>
<p>Log in with the email and password you used during registration.</p>
<p>— IIC Booking</p>""",
            description="Sent when admin approves a user (after final verification). Contains web address so user can start using the online booking facility.",
            variable_help="Variables: {{ name }}, {{ web_address }}.",
            is_active=True,
        )


def remove_registration_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(
        code__in=[
            "registration_self_verification_email",
            "registration_verification_otp_email",
            "registration_approval_confirmation_email",
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0022_booking_created_email_virtual_id_and_wallet_balance"),
    ]

    operations = [
        migrations.RunPython(create_registration_templates, remove_registration_templates),
    ]
