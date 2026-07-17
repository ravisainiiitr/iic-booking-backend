"""
Welcome email helpers.

This module centralizes the subject/body generation so it can be reused by:
- first-login email sending in auth views
- management command for sending a sample welcome email
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils.html import escape


@dataclass(frozen=True)
class WelcomeEmailContent:
    subject: str
    text_body: str
    html_body: str


def build_welcome_email(
    *,
    recipient_name: str | None,
    recipient_email: str,
    user_type: str | None = None,
) -> WelcomeEmailContent:
    """
    Build a styled welcome email.

    Note: user_type is optional; if provided and it looks like a student, the email
    includes a wallet-link CTA (students generally need to join a faculty wallet).
    """
    display_name = (recipient_name or "").strip() or recipient_email
    frontend_url = (getattr(settings, "FRONTEND_URL", "http://localhost:8080") or "").rstrip("/")
    wallet_url = f"{frontend_url}/wallet"
    dashboard_url = f"{frontend_url}/dashboard"

    title = "IIT Roorkee"
    org_name = getattr(settings, "ORG_LEGAL_NAME", "IIT Roorkee")
    student_like = str(user_type or "").lower() in {"student", "individual_student"}
    faculty_like = str(user_type or "").lower() == "faculty"

    subject = f"[{title}] Welcome to IIT Roorkee Portal"

    wallet_para = ""
    if student_like:
        wallet_para = (
            "<p style='margin: 0; color: #243041; font-size: 14px; line-height: 1.6;'>"
            "To start booking equipment, please link your faculty wallet from the Wallet page."
            "</p>"
        )
    elif faculty_like:
        wallet_para = (
            "<p style='margin: 0; color: #243041; font-size: 14px; line-height: 1.6;'>"
            "Thank you for being a faculty wallet owner. You can manage wallet access and approvals in the Wallet page."
            "</p>"
        )

    html_body = f"""<!doctype html>
<html>
  <body style="margin:0; padding:0; background:#f5f7fb;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f7fb;">
      <tr>
        <td align="center" style="padding: 24px 16px;">
          <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="width:100%; max-width:600px;">
            <tr>
              <td style="background: #1d4ed8; padding: 28px 22px; border-radius: 16px 16px 0 0; color: #ffffff;">
                <div style="font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; opacity: 0.95;">
                  {escape(org_name)}
                </div>
                <div style="font-size: 26px; font-weight: 800; margin-top: 8px;">
                  Welcome, {escape(display_name)}!
                </div>
                <div style="font-size: 14px; margin-top: 6px; opacity: 0.95;">
                  {title} is ready for your equipment bookings.
                </div>
              </td>
            </tr>

            <tr>
              <td style="background:#ffffff; padding: 22px;">
                <p style="margin: 0 0 12px 0; color: #243041; font-size: 14px; line-height: 1.6;">
                  Hello! Thank you for logging into the portal for the first time.
                </p>
                {wallet_para}

                <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin: 18px 0 0 0;">
                  <tr>
                    <td bgcolor="#2563eb" style="border-radius: 10px;">
                      <a href="{escape(wallet_url)}"
                         style="display: inline-block; padding: 12px 18px; font-size: 14px; font-weight: 700; color: #ffffff; text-decoration: none; border-radius: 10px;">
                        Go to Wallet
                      </a>
                    </td>
                    <td style="width: 12px;"></td>
                    <td bgcolor="#111827" style="border-radius: 10px;">
                      <a href="{escape(dashboard_url)}"
                         style="display: inline-block; padding: 12px 18px; font-size: 14px; font-weight: 700; color: #ffffff; text-decoration: none; border-radius: 10px;">
                        Open Dashboard
                      </a>
                    </td>
                  </tr>
                </table>

                <p style="margin: 18px 0 0 0; color: #6b7280; font-size: 12px; line-height: 1.6;">
                  If you did not request this email, you can ignore it.
                </p>
              </td>
            </tr>

            <tr>
              <td style="background:#ffffff; padding: 0 22px 22px 22px; border-radius: 0 0 16px 16px;">
                <div style="font-size: 12px; color: #9ca3af; text-align:center; line-height: 1.5;">
                  Powered by IIT Roorkee
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

    text_body_lines = [
        f"Welcome, {display_name}!",
        "",
        "Hello! Thank you for logging into the portal for the first time.",
        wallet_para.replace("<p style='margin: 0; color: #243041; font-size: 14px; line-height: 1.6;'>", "").replace("</p>", "").strip()
        if wallet_para
        else "",
        "",
        f"Wallet: {wallet_url}",
        f"Dashboard: {dashboard_url}",
        "",
        "Powered by IIT Roorkee",
    ]
    text_body = "\n".join([line for line in text_body_lines if line is not None]).strip()

    return WelcomeEmailContent(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )

