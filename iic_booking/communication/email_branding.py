"""
Shared branding, layout, and formatting for portal emails.

Visual reference: welcome_email.py (teal institutional shell).
Used by CommunicationTemplate defaults and styled transactional emails.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from django.conf import settings
from django.utils import timezone
from django.utils.html import escape

PRODUCT_NAME = "Institute Equipment Booking Portal"
PRODUCT_SHORT = "Equipment Booking Portal"
ORG_FALLBACK = "Indian Institute of Technology Roorkee"
CENTRE_LINE = "Institute Instrumentation Centre"

COLOR_BG = "#f0f4f8"
COLOR_SURFACE = "#ffffff"
COLOR_PRIMARY = "#0f766e"
COLOR_PRIMARY_DARK = "#115e59"
COLOR_ACCENT = "#d97706"
COLOR_TEXT = "#0f172a"
COLOR_MUTED = "#64748b"
COLOR_BORDER = "#e2e8f0"
COLOR_CARD = "#f8fafc"
COLOR_FOOTER = "#0f172a"


def org_legal_name() -> str:
    try:
        name = getattr(settings, "ORG_LEGAL_NAME", None)
    except Exception:
        name = None
    return (name or ORG_FALLBACK).strip() or ORG_FALLBACK


def support_contact_line() -> str:
    try:
        support = (getattr(settings, "SUPPORT_EMAIL", None) or "").strip()
    except Exception:
        support = ""
    if support:
        return f"Contact: {support}"
    return "Raise a Support Ticket from the portal after sign-in."


def portal_url() -> str:
    try:
        return (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    except Exception:
        return ""


def user_display_name(user: Any, *, fallback: str = "User") -> str:
    """Prefer a human name; never expose a bare numeric primary key."""
    if user is None:
        return fallback
    if isinstance(user, str):
        text = user.strip()
        if text and not text.isdigit():
            return text
        return fallback
    name = (getattr(user, "name", None) or "").strip()
    if name and not name.isdigit():
        return name
    email = (getattr(user, "email", None) or "").strip()
    if email:
        return email
    return fallback


def format_inr(amount: Any) -> str:
    """Format currency as ₹7,080.00. Empty string when amount is missing/invalid."""
    if amount is None or amount == "":
        return ""
    try:
        if isinstance(amount, Decimal):
            value = amount
        else:
            value = Decimal(str(amount).replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return ""
    quantized = value.quantize(Decimal("0.01"))
    return f"₹{quantized:,.2f}"


def format_duration_minutes(minutes: Any) -> str:
    """Format duration as '1 Hour 30 Minutes' (or minutes-only when under an hour)."""
    if minutes is None or minutes == "":
        return ""
    try:
        total = int(round(float(minutes)))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, mins = divmod(total, 60)
    parts: list[str] = []
    if hours:
        parts.append("1 Hour" if hours == 1 else f"{hours} Hours")
    if mins:
        parts.append("1 Minute" if mins == 1 else f"{mins} Minutes")
    return " ".join(parts)


def format_duration_hours(hours: Any) -> str:
    if hours is None or hours == "":
        return ""
    try:
        minutes = float(hours) * 60.0
    except (TypeError, ValueError):
        return ""
    return format_duration_minutes(minutes)


def _to_local_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Already formatted human strings — leave alone via caller
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    try:
        if timezone.is_aware(dt):
            return timezone.localtime(dt)
        return dt
    except Exception:
        return dt


def format_email_date(value: Any) -> str:
    """24 Jul 2026"""
    dt = _to_local_dt(value)
    if not dt:
        if isinstance(value, str) and value.strip() and "{{" not in value:
            return value.strip()
        return ""
    return dt.strftime("%d %b %Y")


def format_email_time(value: Any) -> str:
    """06:00 AM"""
    dt = _to_local_dt(value)
    if not dt:
        return ""
    return dt.strftime("%I:%M %p")


def format_email_datetime(value: Any) -> str:
    """24 Jul 2026, 06:00 AM"""
    dt = _to_local_dt(value)
    if not dt:
        if isinstance(value, str) and value.strip() and "{{" not in value:
            # Pass through already-friendly strings
            return value.strip()
        return ""
    return f"{dt.strftime('%d %b %Y')}, {dt.strftime('%I:%M %p')}"


def absolute_http_url(url: Any) -> str:
    text = (str(url) if url is not None else "").strip()
    if text.startswith(("http://", "https://")):
        return text
    return ""


def cta_button_html(href: str, label: str, *, primary: bool = True) -> str:
    if not absolute_http_url(href):
        return ""
    bg = COLOR_PRIMARY if primary else COLOR_PRIMARY_DARK
    return f"""
<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:18px 0 6px 0;">
  <tr>
    <td bgcolor="{bg}" style="border-radius:10px;">
      <a href="{escape(href)}"
         style="display:inline-block;padding:12px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:10px;">
        {escape(label)}
      </a>
    </td>
  </tr>
</table>"""


def detail_row_html(label: str, value_placeholder: str) -> str:
    """One detail row; value_placeholder may be literal HTML or {{ var }} / {% if %} blocks."""
    return f"""
<tr>
  <td style="padding:10px 0;border-bottom:1px solid {COLOR_BORDER};font-family:Arial,Helvetica,sans-serif;font-size:12px;color:{COLOR_MUTED};width:38%;vertical-align:top;">
    {escape(label)}
  </td>
  <td style="padding:10px 0;border-bottom:1px solid {COLOR_BORDER};font-family:Arial,Helvetica,sans-serif;font-size:14px;color:{COLOR_TEXT};font-weight:600;vertical-align:top;">
    {value_placeholder}
  </td>
</tr>"""


def optional_detail_row(label: str, var_name: str) -> str:
    return (
        f"{{% if {var_name} %}}"
        + detail_row_html(label, "{{ " + var_name + " }}")
        + f"{{% endif %}}"
    )


def details_card_html(rows: Sequence[str], *, heading: str = "Details") -> str:
    body = "\n".join(rows)
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
       style="background:{COLOR_CARD};border:1px solid {COLOR_BORDER};border-radius:14px;margin:16px 0;">
  <tr>
    <td style="padding:16px 18px 6px 18px;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{COLOR_PRIMARY};">
      {escape(heading)}
    </td>
  </tr>
  <tr>
    <td style="padding:0 18px 10px 18px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        {body}
      </table>
    </td>
  </tr>
</table>"""


def optional_note_block(var_name: str, *, label: str = "Note") -> str:
    return f"""{{% if {var_name} %}}
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:12px 0 4px 0;">
  <tr>
    <td style="padding:14px 16px;background:#ecfdf5;border:1px solid #99f6e4;border-radius:12px;">
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:{COLOR_PRIMARY};margin-bottom:6px;">
        {escape(label)}
      </div>
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:{COLOR_TEXT};white-space:pre-wrap;">{{{{ {var_name} }}}}</div>
    </td>
  </tr>
</table>
{{% endif %}}"""


def optional_cta_block(var_name: str = "link", *, label: str = "View details") -> str:
    return f"""{{% if {var_name} %}}
<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:18px 0 6px 0;">
  <tr>
    <td bgcolor="{COLOR_PRIMARY}" style="border-radius:10px;">
      <a href="{{{{ {var_name} }}}}"
         style="display:inline-block;padding:12px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:10px;">
        {escape(label)}
      </a>
    </td>
  </tr>
</table>
{{% endif %}}"""


def wrap_email_html(
    *,
    title: str,
    body_inner_html: str,
    preheader: str = "",
    subtitle: str = "",
) -> str:
    """Full HTML document matching the Welcome Email visual language."""
    org = org_legal_name()
    portal = portal_url()
    support = support_contact_line()
    portal_line = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#94a3b8;margin-top:8px;">'
        f'Visit: <a href="{escape(portal)}" style="color:#5eead4;text-decoration:none;">{escape(portal)}</a></div>'
        if portal
        else ""
    )
    subtitle_html = (
        f'<div style="font-size:14px;line-height:1.5;color:rgba(255,255,255,0.92);margin-top:10px;">{escape(subtitle)}</div>'
        if subtitle
        else ""
    )
    return f"""<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="light" />
  <meta name="supported-color-schemes" content="light" />
  <title>{escape(title)}</title>
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
      .hero-pad {{ padding: 28px 20px !important; }}
      .body-pad {{ padding: 22px 18px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:{COLOR_BG};">
  <div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">
    {escape(preheader or title)}
  </div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:{COLOR_BG};">
    <tr>
      <td align="center" style="padding:28px 12px;">
        <table role="presentation" class="email-container" width="600" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:600px;">
          <tr>
            <td align="center" style="padding:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:{COLOR_MUTED};">
              {escape(org)}
            </td>
          </tr>
          <tr>
            <td bgcolor="{COLOR_PRIMARY}" class="hero-pad" style="background-color:{COLOR_PRIMARY};background-image:linear-gradient(135deg,#0f766e 0%,#0e7490 55%,#155e75 100%);padding:32px 28px 28px 28px;border-radius:18px 18px 0 0;">
              <div style="display:inline-block;padding:5px 10px;border-radius:999px;background:rgba(255,255,255,0.16);font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">
                {escape(PRODUCT_SHORT)}
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:24px;line-height:1.25;font-weight:800;color:#ffffff;margin-top:14px;">
                {escape(title)}
              </div>
              {subtitle_html}
            </td>
          </tr>
          <tr>
            <td bgcolor="{COLOR_SURFACE}" class="body-pad" style="background-color:{COLOR_SURFACE};padding:28px 28px 24px 28px;border-left:1px solid {COLOR_BORDER};border-right:1px solid {COLOR_BORDER};font-family:Arial,Helvetica,sans-serif;color:{COLOR_TEXT};">
              {body_inner_html}
            </td>
          </tr>
          <tr>
            <td bgcolor="{COLOR_ACCENT}" style="background-color:{COLOR_ACCENT};height:4px;font-size:0;line-height:0;">&nbsp;</td>
          </tr>
          <tr>
            <td bgcolor="{COLOR_FOOTER}" style="background-color:{COLOR_FOOTER};padding:22px 28px;border-radius:0 0 18px 18px;">
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;color:#ffffff;">
                Thank you for using the {escape(PRODUCT_NAME)}.
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#94a3b8;margin-top:8px;">
                {escape(CENTRE_LINE)}<br/>{escape(org)}
              </div>
              <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#94a3b8;margin-top:10px;">
                Need assistance?<br/>{escape(support)}
              </div>
              {portal_line}
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:16px 8px 0 8px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;">
              &copy; {escape(org)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def branded_plain_footer() -> str:
    org = org_legal_name()
    portal = portal_url()
    lines = [
        "",
        f"Thank you for using the {PRODUCT_NAME}.",
        CENTRE_LINE,
        org,
        "",
        "Need assistance?",
        support_contact_line(),
    ]
    if portal:
        lines.append(f"Visit: {portal}")
    return "\n".join(lines)


def greeting_html(name_var: str = "user_name") -> str:
    return (
        f'<p style="margin:0 0 14px 0;font-size:15px;line-height:1.6;color:{COLOR_TEXT};">'
        f"Hello {{{{ {name_var} }}}},"
        f"</p>"
    )


def paragraph_html(text: str) -> str:
    """Static paragraph (may include {{ placeholders }})."""
    return (
        f'<p style="margin:0 0 14px 0;font-size:14px;line-height:1.65;color:{COLOR_TEXT};">'
        f"{text}"
        f"</p>"
    )


def booking_details_rows(
    *,
    include_status: bool = False,
    include_wallet: bool = False,
    include_payment: bool = False,
    include_duration: bool = True,
    include_charges: bool = True,
) -> list[str]:
    rows = [
        optional_detail_row("Booking ID", "booking_id"),
        optional_detail_row("Equipment", "equipment_name"),
        optional_detail_row("Equipment code", "equipment_code"),
        optional_detail_row("Start time", "start_time"),
        optional_detail_row("End time", "end_time"),
        optional_detail_row("Booking date", "booking_date"),
        optional_detail_row("Slot(s)", "slot_id_display"),
    ]
    if include_duration:
        rows.append(optional_detail_row("Duration", "duration_display"))
    if include_charges:
        rows.append(optional_detail_row("Total charges", "total_charge"))
    if include_wallet:
        rows.append(optional_detail_row("Wallet balance", "wallet_balance_after"))
    if include_status:
        rows.append(optional_detail_row("Booking status", "new_status"))
        rows.append(optional_detail_row("Previous status", "previous_status"))
    if include_payment:
        rows.append(optional_detail_row("Payment mode", "payment_mode"))
    return rows


def build_standard_email(
    *,
    title: str,
    subject: str,
    intro: str,
    detail_rows: Optional[Sequence[str]] = None,
    details_heading: str = "Details",
    note_vars: Optional[Sequence[tuple[str, str]]] = None,
    cta_label: str = "View details",
    cta_var: str = "link",
    extra_html: str = "",
    preheader: str = "",
    subtitle: str = "",
    name_var: str = "user_name",
    variable_help: str = "",
    description: str = "",
) -> dict[str, str]:
    """
    Build subject / body_text / body_html for a CommunicationTemplate row.
    detail_rows: prebuilt row HTML snippets (may include {% if %} wrappers).
    note_vars: list of (var_name, label).
    """
    parts = [greeting_html(name_var), paragraph_html(intro)]
    if detail_rows:
        parts.append(details_card_html(detail_rows, heading=details_heading))
    for var_name, label in note_vars or ():
        parts.append(optional_note_block(var_name, label=label))
    if extra_html:
        parts.append(extra_html)
    parts.append(optional_cta_block(cta_var, label=cta_label))
    body_inner = "\n".join(parts)
    html = wrap_email_html(
        title=title,
        body_inner_html=body_inner,
        preheader=preheader or title,
        subtitle=subtitle,
    )

    text_lines = [
        f"Hello {{{{ {name_var} }}}},",
        "",
        intro.replace("<strong>", "").replace("</strong>", ""),
        "",
    ]
    if detail_rows:
        text_lines.append(f"{details_heading}:")
        # Plain-text mirror uses common vars when present
        for label, var in (
            ("Booking ID", "booking_id"),
            ("Equipment", "equipment_name"),
            ("Start time", "start_time"),
            ("End time", "end_time"),
            ("Duration", "duration_display"),
            ("Total charges", "total_charge"),
            ("Wallet balance", "wallet_balance_after"),
            ("Status", "new_status"),
        ):
            text_lines.append(f"{{% if {var} %}}- {label}: {{{{ {var} }}}}{{% endif %}}")
        text_lines.append("")
    for var_name, label in note_vars or ():
        text_lines.append(f"{{% if {var_name} %}}{label}:\n{{{{ {var_name} }}}}\n{{% endif %}}")
    text_lines.append(f"{{% if {cta_var} %}}{cta_label}: {{{{ {cta_var} }}}}{{% endif %}}")
    text_lines.append(branded_plain_footer())
    body_text = "\n".join(text_lines)

    return {
        "subject": subject,
        "body_html": html,
        "body_text": body_text,
        "variable_help": variable_help,
        "description": description,
        "name": title,
    }


def sanitize_template_context(context: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize common fields before render (safe defaults, no raw PK names)."""
    ctx = dict(context or {})
    for key in ("user_name", "booked_for_user_name", "faculty_name", "student_name", "operator_name"):
        if key in ctx:
            ctx[key] = user_display_name(ctx.get(key), fallback="")
            if not ctx[key]:
                ctx[key] = ""
    if "comment" in ctx:
        comment = str(ctx.get("comment") or "").strip()
        if comment.lower() in ("no comment", "none", "null", "-"):
            comment = ""
        ctx["comment"] = comment
    if "link" in ctx:
        ctx["link"] = absolute_http_url(ctx.get("link"))
    # Friendly aliases
    if not ctx.get("duration_display"):
        if ctx.get("total_time_minutes") not in (None, ""):
            ctx["duration_display"] = format_duration_minutes(ctx.get("total_time_minutes"))
        elif ctx.get("total_hours") not in (None, ""):
            ctx["duration_display"] = format_duration_hours(ctx.get("total_hours"))
    if "total_charge" in ctx and ctx["total_charge"] not in (None, ""):
        raw = str(ctx["total_charge"])
        if not raw.strip().startswith("₹"):
            formatted = format_inr(raw)
            if formatted:
                ctx["total_charge"] = formatted
    for money_key in ("amount", "balance", "threshold", "credit_limit_amount", "refund_amount", "extra_amount"):
        if money_key in ctx and ctx[money_key] not in (None, ""):
            raw = str(ctx[money_key])
            if not raw.strip().startswith("₹"):
                formatted = format_inr(raw)
                if formatted:
                    ctx[money_key] = formatted
    if "wallet_balance_after" in ctx and ctx["wallet_balance_after"] not in (None, "", "N/A"):
        raw = str(ctx["wallet_balance_after"])
        if not raw.strip().startswith("₹"):
            formatted = format_inr(raw)
            if formatted:
                ctx["wallet_balance_after"] = formatted
    elif ctx.get("wallet_balance_after") == "N/A":
        ctx["wallet_balance_after"] = ""
    return ctx
