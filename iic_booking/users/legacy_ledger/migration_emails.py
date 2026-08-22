"""Phase 8C — role-specific migration HTML emails (Outlook/Gmail-safe tables)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils.html import escape

from iic_booking.communication.email_branding import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_MUTED,
    COLOR_PRIMARY_DARK,
    COLOR_SURFACE,
    COLOR_TEXT,
    org_legal_name,
    wrap_email_html,
)
from iic_booking.users.models.portal_migration import MigrationNotificationTemplate
from iic_booking.users.models.user_type import UserType

# Spec primary navy for migration hero accents
MIGRATION_NAVY = "#1D2844"


@dataclass(frozen=True)
class MigrationEmailContent:
    template: str
    subject: str
    preheader: str
    text_body: str
    html_body: str


SUBJECTS = {
    MigrationNotificationTemplate.FACULTY_MIGRATION: (
        "IIC Booking Portal has moved to a new platform — "
        "Please use the new portal for future bookings"
    ),
    MigrationNotificationTemplate.STUDENT_MIGRATION: (
        "New IIC Booking Portal is now live — "
        "Use the new portal for all future bookings"
    ),
    MigrationNotificationTemplate.OIC_MIGRATION: (
        "IIC Booking Portal Migration — Action Required for Officers-in-Charge"
    ),
    MigrationNotificationTemplate.ADMIN_MIGRATION: (
        "IIC Booking Portal Migration — Main Administrator operational briefing"
    ),
}


def classify_migration_template(user) -> tuple[str | None, str]:
    """Return (template_code, role_label) or (None, reason) if unsupported/ambiguous."""
    ut = str(getattr(user, "user_type", "") or "").strip().lower()
    if not ut:
        return None, "missing_user_type"
    if ut == UserType.ADMIN:
        return MigrationNotificationTemplate.ADMIN_MIGRATION, "admin"
    if ut == UserType.MANAGER:
        return MigrationNotificationTemplate.OIC_MIGRATION, "oic"
    if ut == UserType.FACULTY:
        return MigrationNotificationTemplate.FACULTY_MIGRATION, "faculty"
    if ut in {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT}:
        return MigrationNotificationTemplate.STUDENT_MIGRATION, "student"
    # Explicit skip list — report rather than wrong template
    if ut in {
        UserType.OPERATOR,
        UserType.DEPT_ADMIN,
        UserType.FINANCE,
        UserType.ORG_ADMIN,
        UserType.EXTERNAL_RELATIONS,
        UserType.EXTERNAL,
        UserType.RND.lower() if isinstance(UserType.RND, str) else "rnd",
        "rnd",
        UserType.INSTITUTE.lower() if isinstance(UserType.INSTITUTE, str) else "industry",
        "industry",
        UserType.STARTUP_INCUBATED_IITR,
        UserType.EXTERNAL_STARTUP_MSME,
        UserType.OTHER,
    }:
        return None, f"unsupported_role:{ut}"
    return None, f"ambiguous_role:{ut}"


def _ctx(**kwargs) -> dict[str, str]:
    support_email = (
        kwargs.get("support_email")
        or getattr(settings, "SUPPORT_EMAIL", "")
        or "support@example.invalid"
    )
    support_phone = kwargs.get("support_phone") or getattr(settings, "SUPPORT_PHONE", "") or ""
    portal_name = kwargs.get("portal_name") or "IIC Booking Portal"
    return {
        "user_name": kwargs.get("user_name") or "User",
        "new_portal_url": kwargs.get("new_portal_url") or "",
        "migration_datetime": kwargs.get("migration_datetime") or "",
        "support_email": support_email,
        "support_phone": support_phone,
        "portal_name": portal_name,
    }


def _feature_cards(items: list[tuple[str, str]]) -> str:
    rows = []
    for title, body in items:
        rows.append(
            f"""
            <td width="50%" valign="top" style="padding:6px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="background:{COLOR_CARD};border:1px solid {COLOR_BORDER};border-radius:10px;">
                <tr><td style="padding:14px 16px;">
                  <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;color:{MIGRATION_NAVY};">{escape(title)}</div>
                  <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.5;color:{COLOR_MUTED};margin-top:6px;">{escape(body)}</div>
                </td></tr>
              </table>
            </td>"""
        )
    # pair into rows of 2
    html = []
    for i in range(0, len(rows), 2):
        pair = rows[i : i + 2]
        if len(pair) == 1:
            pair.append("<td width='50%'></td>")
        html.append(f"<tr>{''.join(pair)}</tr>")
    return f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{''.join(html)}</table>"


def _cta(url: str, label: str) -> str:
    if not url:
        return (
            f"<p style='font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{COLOR_MUTED};'>"
            "New portal URL will be provided by your administrator.</p>"
        )
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin:18px 0 8px 0;'>"
        f"<tr><td style='border-radius:8px;background:{MIGRATION_NAVY};'>"
        f"<a href='{escape(url)}' style='display:inline-block;padding:14px 22px;font-family:Arial,Helvetica,sans-serif;"
        f"font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;'>{escape(label)}</a>"
        f"</td></tr></table>"
    )


def _instructions_block(title: str, bullets: list[str]) -> str:
    lis = "".join(
        f"<li style='margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:{COLOR_TEXT};'>{escape(b)}</li>"
        for b in bullets
    )
    return (
        f"<div style='margin:18px 0;padding:16px;border:1px solid {COLOR_BORDER};border-radius:10px;background:{COLOR_SURFACE};'>"
        f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;color:{MIGRATION_NAVY};margin-bottom:10px;'>{escape(title)}</div>"
        f"<ul style='margin:0;padding-left:18px;'>{lis}</ul></div>"
    )


def build_migration_email(template: str, **kwargs) -> MigrationEmailContent:
    c = _ctx(**kwargs)
    subject = SUBJECTS.get(template, "IIC Booking Portal Migration")
    preheader = (
        f"Migration effective {c['migration_datetime']}. "
        f"Use the new portal for future bookings."
    )
    hero = (
        f"<p style='margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:16px;color:{COLOR_TEXT};'>"
        f"Dear {escape(c['user_name'])},</p>"
        f"<p style='margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:{COLOR_TEXT};'>"
        f"The <strong>{escape(c['portal_name'])}</strong> has migrated to a new platform. "
        f"Migration effective: <strong>{escape(c['migration_datetime'])}</strong>."
        f"</p>"
    )
    common_cards = _feature_cards(
        [
            ("Easier equipment discovery", "Find instruments across departments with clearer profiles."),
            ("Live availability", "See calendar availability before you book."),
            ("Booking management", "Track upcoming bookings and status updates in one place."),
            ("History & wallet", "Continue to view booking history and account/wallet information."),
        ]
    )
    cta = _cta(c["new_portal_url"], "Access New IIC Booking Portal")
    support = (
        f"<div style='margin-top:20px;padding-top:14px;border-top:1px solid {COLOR_BORDER};'>"
        f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{COLOR_MUTED};'>"
        f"Support: {escape(c['support_email'])}"
        + (f" · {escape(c['support_phone'])}" if c["support_phone"] else "")
        + f"<br/>{escape(org_legal_name())}</div></div>"
    )

    if template == MigrationNotificationTemplate.FACULTY_MIGRATION:
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>The new portal improves equipment discovery, availability visibility, booking management, "
            "booking history, and wallet/account information, with Channel-I authentication and a foundation "
            "for future IIC platform enhancements.</p>"
            + common_cards
            + _instructions_block(
                "Important for Faculty",
                [
                    "All NEW bookings must be made through the NEW portal.",
                    "The old portal remains available during transition for existing bookings, history, and account information.",
                    "Sign in with Channel-I on the new portal.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"{c['portal_name']} has moved. Effective: {c['migration_datetime']}.\n"
            "All NEW bookings must use the NEW portal.\n"
            f"New portal: {c['new_portal_url']}\n"
            f"Support: {c['support_email']}\n"
        )
    elif template == MigrationNotificationTemplate.STUDENT_MIGRATION:
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>Use the new portal for equipment discovery, availability, booking, booking tracking, "
            "and account/wallet information with Channel-I login.</p>"
            + common_cards
            + _instructions_block(
                "Important for Students",
                [
                    "New bookings cannot be created through the old portal.",
                    "Use the new IIC Booking Portal for all future bookings.",
                    "You can still view previous bookings and account information on the old portal during transition.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"New IIC Booking Portal is live. Effective: {c['migration_datetime']}.\n"
            "New bookings cannot be created on the old portal.\n"
            f"New portal: {c['new_portal_url']}\n"
        )
    elif template == MigrationNotificationTemplate.OIC_MIGRATION:
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>As Officer-in-Charge, please review legacy bookings, continue operational status management, "
            "and use the new portal for all new bookings. Legacy booking slots are protected in the new portal "
            "to prevent duplicate booking during migration.</p>"
            + common_cards
            + _instructions_block(
                "Action required — OIC",
                [
                    "You may continue operational handling of eligible legacy bookings.",
                    "You may issue eligible one-time migration refunds (settlement).",
                    "You cannot create new bookings on the old portal.",
                    "All new bookings must be created on the new portal.",
                    "Protected legacy slots will show as unavailable in the new portal during migration.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"OIC migration briefing. Effective: {c['migration_datetime']}.\n"
            "Operational legacy handling YES; migration refund YES; old-portal new booking NO.\n"
            f"New portal: {c['new_portal_url']}\n"
        )
    else:  # ADMIN_MIGRATION
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>As Main Administrator you retain global visibility across departments and equipment, "
            "mapping control, migration control, and migration refund authority. New bookings must use the new portal; "
            "legacy slots remain protected until released.</p>"
            + common_cards
            + _instructions_block(
                "Main Administrator",
                [
                    "Global department/equipment/mapping/block visibility remains available.",
                    "Old-portal new booking is disabled during freeze.",
                    "Migration refund authority remains available for eligible bookings.",
                    "Do not activate production T0 from this staging communication.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"Main Administrator migration briefing. Effective: {c['migration_datetime']}.\n"
            f"New portal: {c['new_portal_url']}\n"
        )

    html = wrap_email_html(
        title=c["portal_name"] + " — Migration",
        subtitle="Please use the new portal for future bookings",
        body_inner_html=body,
        preheader=preheader,
    )
    # Soft accent strip (inline, no external image)
    html = html.replace(
        COLOR_PRIMARY_DARK,
        MIGRATION_NAVY,
        1,
    )
    return MigrationEmailContent(
        template=template,
        subject=subject,
        preheader=preheader,
        text_body=text,
        html_body=html,
    )


def preview_sample_context(template: str) -> dict[str, Any]:
    """Sample/test data for Main Admin preview — no real PII."""
    samples = {
        MigrationNotificationTemplate.FACULTY_MIGRATION: "Faculty Preview User",
        MigrationNotificationTemplate.STUDENT_MIGRATION: "Student Preview User",
        MigrationNotificationTemplate.OIC_MIGRATION: "OIC Preview User",
        MigrationNotificationTemplate.ADMIN_MIGRATION: "Administrator Preview User",
    }
    return {
        "user_name": samples.get(template, "Preview User"),
        "new_portal_url": "https://staging.example.invalid/new-portal",
        "migration_datetime": "01 September 2026, 09:00 IST",
        "support_email": "staging-support@example.invalid",
        "support_phone": "+91-0000-000000",
        "portal_name": "IIC Booking Portal",
    }
