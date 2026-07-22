"""
First-login welcome email for the Institute Equipment Booking Portal.

Table-based HTML with inline CSS for Gmail, Outlook, Apple Mail, and mobile clients.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils.html import escape


PRODUCT_NAME = "Institute Equipment Booking Portal"
PRODUCT_SHORT = "Equipment Booking Portal"

# Teal corporate palette aligned with the portal UI / IIT Roorkee institutional tone
_COLOR_BG = "#f0f4f8"
_COLOR_SURFACE = "#ffffff"
_COLOR_PRIMARY = "#0f766e"
_COLOR_PRIMARY_DARK = "#115e59"
_COLOR_ACCENT = "#d97706"
_COLOR_TEXT = "#0f172a"
_COLOR_MUTED = "#64748b"
_COLOR_BORDER = "#e2e8f0"
_COLOR_CARD = "#f8fafc"


@dataclass(frozen=True)
class WelcomeEmailContent:
    subject: str
    text_body: str
    html_body: str


def _user_type_label(user_type: str | None, user_type_alias: str | None = None) -> str:
    alias = (user_type_alias or "").strip()
    if alias:
        return alias
    code = str(user_type or "").strip().lower()
    labels = {
        "admin": "Institute Administrator",
        "dept_admin": "Department Administrator",
        "manager": "Officer In Charge",
        "operator": "Lab In-Charge",
        "finance": "Accounts In Charge",
        "external_relations": "External Relations Administrator",
        "org_admin": "Organization Administrator",
        "student": "IIT Roorkee Student",
        "individual_student": "Individual Student",
        "faculty": "IIT Roorkee Faculty",
        "external": "Educational Institute",
        "rnd": "Govt R&D Organization",
        "industry": "Industry",
        "startup_incubated_iitr": "Startup Incubated at IIT Roorkee",
        "external_startup_msme": "External Startup / MSME",
        "other": "External User",
    }
    return labels.get(code, code.replace("_", " ").title() if code else "Portal User")


def _role_welcome_line(user_type: str | None) -> str:
    code = str(user_type or "").strip().lower()
    messages = {
        "faculty": (
            "As faculty, you can book at internal rates, fund your research group through department "
            "wallets, and approve student wallet access — all from one secure dashboard."
        ),
        "student": (
            "As an IIT Roorkee student, link to your faculty wallet to book instruments, track samples, "
            "and follow booking status with clear notifications every step of the way."
        ),
        "individual_student": (
            "Your account is ready for instrument bookings with transparent charges, wallet workflows, "
            "and full visibility of sample and booking status."
        ),
        "dept_admin": (
            "As Department Administrator, you oversee staff roles, department settings, and the tools "
            "that keep your laboratories running smoothly on the portal."
        ),
        "manager": (
            "As Officer In Charge, manage equipment calendars, booking operations, and lab workflows "
            "with the controls designed for day-to-day research facility leadership."
        ),
        "operator": (
            "As Lab In-Charge, complete runs, update booking status, and keep the instrument calendar "
            "accurate for researchers who depend on your lab."
        ),
        "finance": (
            "As Accounts In Charge, process payment receipts and keep department finance workflows "
            "aligned with bookings and wallet activity."
        ),
        "external": (
            "As an external educational user, explore published equipment, request bookings with clear "
            "charges, and follow your sample journey through to results."
        ),
        "rnd": (
            "As a Govt R&D user, book instruments with transparent workflows, digital payments, and "
            "full status visibility designed for research collaboration."
        ),
        "industry": (
            "As an industry user, access institute facilities through structured booking, payment, and "
            "sample-tracking workflows built for professional research partnerships."
        ),
        "startup_incubated_iitr": (
            "As a campus-incubated startup, use the portal to book instruments efficiently and keep "
            "your experimental work moving with clear digital workflows."
        ),
        "external_startup_msme": (
            "As a startup / MSME user, book equipment with transparent pricing and track every step "
            "from reservation to sample return."
        ),
        "admin": (
            "As Institute Administrator, you have full oversight of the portal that powers research "
            "infrastructure booking across IIT Roorkee."
        ),
        "external_relations": (
            "As External Relations Administrator, support verification and onboarding so external "
            "partners can access institute facilities with confidence."
        ),
    }
    return messages.get(
        code,
        (
            "You now have access to a unified digital platform for booking instruments, managing "
            "approvals and payments, and tracking research work across IIT Roorkee laboratories."
        ),
    )


_FEATURES: list[tuple[str, str, str]] = [
    ("🏠", "Your personalized dashboard", "All test results, booking updates, approvals, notifications, invoices, and complete activity history live in one place — visit regularly."),
    ("📅", "Centralized booking", "Reserve instruments across departments from one calendar."),
    ("✅", "Transparent approvals", "Clear online workflows for requests and authorisations."),
    ("💳", "Digital wallets", "Grant-based payments and department sub-wallet management."),
    ("🧪", "Sample lifecycle", "Track samples from submission through analysis and return."),
    ("🔔", "Live status & alerts", "Real-time booking updates by email and in-app notices."),
    ("🗓️", "Availability calendar", "See open slots before you commit to a booking."),
    ("🔐", "Role-based access", "Secure permissions tailored to your responsibilities."),
]


def _feature_card(icon: str, title: str, body: str) -> str:
    return f"""
      <td width="50%" valign="top" style="padding:6px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{_COLOR_CARD};border:1px solid {_COLOR_BORDER};border-radius:12px;">
          <tr>
            <td style="padding:14px 14px 16px 14px;">
              <div style="font-size:20px;line-height:1;margin-bottom:8px;">{icon}</div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:{_COLOR_TEXT};margin-bottom:4px;">{escape(title)}</div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.45;color:{_COLOR_MUTED};">{escape(body)}</div>
            </td>
          </tr>
        </table>
      </td>"""


def _features_table() -> str:
    rows = []
    for i in range(0, len(_FEATURES), 2):
        left = _FEATURES[i]
        right = _FEATURES[i + 1] if i + 1 < len(_FEATURES) else None
        left_html = _feature_card(*left)
        right_html = _feature_card(*right) if right else '<td width="50%" style="padding:6px;"></td>'
        rows.append(f"<tr>{left_html}{right_html}</tr>")
    return f"""
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0;">
      {''.join(rows)}
    </table>"""


def _cta_button(href: str, label: str, *, primary: bool = True) -> str:
    bg = _COLOR_PRIMARY if primary else _COLOR_PRIMARY_DARK
    return f"""
      <td align="center" style="padding:4px;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0">
          <tr>
            <td bgcolor="{bg}" style="border-radius:10px;">
              <a href="{escape(href)}"
                 style="display:inline-block;padding:12px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:10px;">
                {escape(label)}
              </a>
            </td>
          </tr>
        </table>
      </td>"""


def build_welcome_email(
    *,
    recipient_name: str | None,
    recipient_email: str,
    user_type: str | None = None,
    user_type_alias: str | None = None,
    department_name: str | None = None,
) -> WelcomeEmailContent:
    """
    Build a premium first-login welcome email.

    Personalization: display name, role label, optional department, role-specific welcome line.
    """
    display_name = (recipient_name or "").strip() or recipient_email
    role_label = _user_type_label(user_type, user_type_alias)
    dept = (department_name or "").strip()
    role_line = _role_welcome_line(user_type)

    frontend_url = (getattr(settings, "FRONTEND_URL", "http://localhost:8080") or "").rstrip("/")
    dashboard_url = f"{frontend_url}/dashboard"
    guide_url = f"{frontend_url}/dashboard"
    book_url = f"{frontend_url}/equipments"
    org_name = getattr(settings, "ORG_LEGAL_NAME", None) or "Indian Institute of Technology Roorkee"

    subject = f"Welcome to the {PRODUCT_NAME} — {display_name}"

    meta_bits = [escape(role_label)]
    if dept:
        meta_bits.append(escape(dept))
    meta_html = " · ".join(meta_bits)

    preheader = (
        f"Welcome aboard, {display_name}. Your dashboard is the hub for bookings, sample progress, "
        f"test reports, wallet activity, and notifications at IIT Roorkee."
    )

    html_body = f"""<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="light" />
  <meta name="supported-color-schemes" content="light" />
  <title>{escape(PRODUCT_NAME)} — Welcome</title>
  <!--[if mso]>
  <noscript>
    <xml>
      <o:OfficeDocumentSettings>
        <o:PixelsPerInch>96</o:PixelsPerInch>
      </o:OfficeDocumentSettings>
    </xml>
  </noscript>
  <![endif]-->
  <style type="text/css">
    body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    img {{ -ms-interpolation-mode: bicubic; border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; }}
    body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; }}
    a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important; }}
    @media only screen and (max-width: 620px) {{
      .email-container {{ width: 100% !important; max-width: 100% !important; }}
      .stack-column {{ display: block !important; width: 100% !important; max-width: 100% !important; }}
      .hero-pad {{ padding: 28px 20px !important; }}
      .body-pad {{ padding: 22px 18px !important; }}
      .cta-stack td {{ display: block !important; width: 100% !important; }}
      .cta-stack a {{ display: block !important; width: 100% !important; box-sizing: border-box !important; text-align: center !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:{_COLOR_BG};">
  <!-- Preheader -->
  <div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">
    {escape(preheader)}
  </div>

  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:{_COLOR_BG};">
    <tr>
      <td align="center" style="padding:28px 12px;">
        <table role="presentation" class="email-container" width="600" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:600px;">

          <!-- Top brand bar -->
          <tr>
            <td align="center" style="padding:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:{_COLOR_MUTED};">
              {escape(org_name)}
            </td>
          </tr>

          <!-- Hero -->
          <tr>
            <td bgcolor="{_COLOR_PRIMARY}" class="hero-pad" style="background-color:{_COLOR_PRIMARY};background-image:linear-gradient(135deg,#0f766e 0%,#0e7490 55%,#155e75 100%);padding:36px 28px 32px 28px;border-radius:18px 18px 0 0;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="font-family:Arial,Helvetica,sans-serif;">
                    <div style="display:inline-block;padding:5px 10px;border-radius:999px;background:rgba(255,255,255,0.16);font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#ffffff;">
                      {escape(PRODUCT_SHORT)}
                    </div>
                    <div style="font-size:26px;line-height:1.25;font-weight:800;color:#ffffff;margin-top:16px;">
                      Welcome aboard, {escape(display_name)}
                    </div>
                    <div style="font-size:15px;line-height:1.5;color:rgba(255,255,255,0.92);margin-top:10px;max-width:480px;">
                      You are joining a modern research facility platform built to make instrument booking at IIT Roorkee simpler, clearer, and more reliable.
                    </div>
                    <div style="margin-top:16px;font-size:12px;font-weight:600;color:#fde68a;">
                      {meta_html}
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td bgcolor="{_COLOR_SURFACE}" class="body-pad" style="background-color:{_COLOR_SURFACE};padding:28px 28px 8px 28px;border-left:1px solid {_COLOR_BORDER};border-right:1px solid {_COLOR_BORDER};">
              <p style="margin:0 0 14px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:{_COLOR_TEXT};">
                Hello {escape(display_name)},
              </p>
              <p style="margin:0 0 14px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:{_COLOR_TEXT};">
                The <strong>{escape(PRODUCT_NAME)}</strong> is a comprehensive digital ecosystem for research,
                testing, and academic laboratories at IIT Roorkee — bringing equipment calendars, approvals,
                wallets, and sample tracking together in one thoughtfully engineered experience.
              </p>
              <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:{_COLOR_TEXT};">
                {escape(role_line)}
              </p>
            </td>
          </tr>

          <!-- Dashboard hub callout -->
          <tr>
            <td bgcolor="{_COLOR_SURFACE}" style="background-color:{_COLOR_SURFACE};padding:8px 28px 6px 28px;border-left:1px solid {_COLOR_BORDER};border-right:1px solid {_COLOR_BORDER};">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:linear-gradient(135deg,#ecfdf5 0%,#f0f9ff 100%);border:1px solid #99f6e4;border-radius:14px;">
                <tr>
                  <td style="padding:18px 18px 20px 18px;">
                    <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{_COLOR_PRIMARY};">
                      Your command centre
                    </div>
                    <div style="font-family:Arial,Helvetica,sans-serif;font-size:17px;font-weight:800;color:{_COLOR_TEXT};margin-top:6px;">
                      Everything lives on your dashboard
                    </div>
                    <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:{_COLOR_TEXT};margin-top:8px;">
                      All <strong>test results</strong>, <strong>booking updates</strong>, <strong>approvals</strong>,
                      <strong>notifications</strong>, <strong>invoices</strong>, and your complete
                      <strong>activity history</strong> are available from your personalized dashboard.
                      Visit it regularly — it is the central hub for managing equipment bookings, monitoring
                      sample progress, accessing test reports, tracking financial transactions, and receiving
                      important notifications.
                    </div>
                    <div style="margin-top:14px;">
                      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                        <tr>
                          <td bgcolor="{_COLOR_PRIMARY}" style="border-radius:10px;">
                            <a href="{escape(dashboard_url)}"
                               style="display:inline-block;padding:11px 16px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:10px;">
                              Open your dashboard
                            </a>
                          </td>
                        </tr>
                      </table>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Features heading -->
          <tr>
            <td bgcolor="{_COLOR_SURFACE}" style="background-color:{_COLOR_SURFACE};padding:18px 28px 6px 28px;border-left:1px solid {_COLOR_BORDER};border-right:1px solid {_COLOR_BORDER};">
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{_COLOR_PRIMARY};">
                What you can do
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:18px;font-weight:800;color:{_COLOR_TEXT};margin-top:6px;">
                Built for research productivity
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.5;color:{_COLOR_MUTED};margin-top:6px;">
                Designed specifically for research, testing, and academic laboratories.
              </div>
            </td>
          </tr>

          <tr>
            <td bgcolor="{_COLOR_SURFACE}" style="background-color:{_COLOR_SURFACE};padding:10px 22px 8px 22px;border-left:1px solid {_COLOR_BORDER};border-right:1px solid {_COLOR_BORDER};">
              {_features_table()}
            </td>
          </tr>

          <!-- CTAs -->
          <tr>
            <td bgcolor="{_COLOR_SURFACE}" style="background-color:{_COLOR_SURFACE};padding:22px 28px 28px 28px;border-left:1px solid {_COLOR_BORDER};border-right:1px solid {_COLOR_BORDER};">
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:{_COLOR_TEXT};margin-bottom:12px;">
                Ready when you are
              </div>
              <table role="presentation" class="cta-stack" cellspacing="0" cellpadding="0" border="0" align="left">
                <tr>
                  {_cta_button(dashboard_url, "Open Dashboard", primary=True)}
                  {_cta_button(guide_url, "Explore User Guide", primary=False)}
                  {_cta_button(book_url, "Book Equipment", primary=False)}
                </tr>
              </table>
              <div style="clear:both;height:0;line-height:0;font-size:0;">&nbsp;</div>
              <p style="margin:18px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:{_COLOR_MUTED};">
                Tip: after you sign in, open <strong style="color:{_COLOR_TEXT};">User Guide</strong> from the menu
                anytime for a role-specific walkthrough of the portal.
              </p>
            </td>
          </tr>

          <!-- Accent strip -->
          <tr>
            <td bgcolor="{_COLOR_ACCENT}" style="background-color:{_COLOR_ACCENT};height:4px;font-size:0;line-height:0;">&nbsp;</td>
          </tr>

          <!-- Footer -->
          <tr>
            <td bgcolor="#0f172a" style="background-color:#0f172a;padding:22px 28px;border-radius:0 0 18px 18px;">
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;">
                {escape(PRODUCT_NAME)}
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#94a3b8;margin-top:6px;">
                {escape(org_name)}
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#94a3b8;margin-top:10px;">
                Need help? Raise a Support Ticket from the portal after sign-in, or contact your department&rsquo;s
                Officer In Charge for equipment-specific guidance.
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.5;color:#64748b;margin-top:14px;">
                This message was sent to {escape(recipient_email)} because you signed in for the first time.
                If you did not expect this email, you can safely ignore it.
              </div>
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:16px 8px 0 8px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;">
              &copy; {escape(org_name)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    text_lines = [
        f"Welcome aboard, {display_name}!",
        "",
        f"You are joining the {PRODUCT_NAME} at IIT Roorkee — a comprehensive digital ecosystem for",
        "research infrastructure booking, approvals, wallets, and sample tracking.",
        "",
        f"Your role: {role_label}" + (f" | Department: {dept}" if dept else ""),
        "",
        role_line,
        "",
        "YOUR DASHBOARD — the central hub:",
        "All test results, booking updates, approvals, notifications, invoices, and complete activity",
        "history are available from your personalized dashboard. Visit regularly to manage bookings,",
        "monitor sample progress, access test reports, track financial transactions, and receive notices.",
        f"Open Dashboard: {dashboard_url}",
        "",
        "Key capabilities:",
        *[f"- {title}: {body}" for _, title, body in _FEATURES],
        "",
        f"Open Dashboard: {dashboard_url}",
        f"Explore User Guide: {guide_url} (open User Guide from the menu after sign-in)",
        f"Book Equipment: {book_url}",
        "",
        f"— {PRODUCT_NAME}",
        org_name,
    ]
    text_body = "\n".join(text_lines)

    return WelcomeEmailContent(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def welcome_email_kwargs_from_user(user) -> dict:
    """Collect personalization fields from a User instance for build_welcome_email."""
    dept_name = None
    try:
        department = getattr(user, "department", None)
        if department is not None:
            dept_name = getattr(department, "name", None)
    except Exception:
        dept_name = None
    return {
        "recipient_name": getattr(user, "name", None),
        "recipient_email": user.email,
        "user_type": getattr(user, "user_type", None),
        "user_type_alias": getattr(user, "user_type_alias", None),
        "department_name": dept_name,
    }
