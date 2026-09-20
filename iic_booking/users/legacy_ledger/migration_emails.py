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
        "New IIC Equipment Booking Portal — Booking opens Wed 30 Sep 2026, 9:00 PM | "
        "Action required for IITR Faculty"
    ),
    MigrationNotificationTemplate.STUDENT_MIGRATION: (
        "New IIC Equipment Booking Portal — Booking opens Wed 30 Sep 2026, 9:00 PM | "
        "Information for IITR Students"
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
        "Booking for week commencing 05 October 2026 uses the new portal. "
        "Booking opens as usual Wednesday, 30 September 2026 at 9:00 PM."
    )
    hero = (
        f"<p style='margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:16px;color:{COLOR_TEXT};'>"
        f"Dear {escape(c['user_name'])},</p>"
        f"<p style='margin:0 0 12px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:{COLOR_TEXT};'>"
        f"The Institute Instrumentation Centre (IIC), IIT Roorkee is launching the new "
        f"<strong>{escape(c['portal_name'])}</strong>. "
        f"Booking for the week commencing from <strong>05 October 2026</strong> will be accepted using the new booking portal. "
        f"Booking will be opened as usual on <strong>Wednesday, 30 September 2026 at 9:00 PM</strong>."
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
    cta = _cta(c["new_portal_url"], "Open New IIC Booking Portal")
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
            + ";'>This message is for <strong>IITR Faculty</strong>. Please review the steps below so your "
            "wallet balance, transaction history, and student linking are ready when booking opens.</p>"
            + common_cards
            + _instructions_block(
                "Important for IITR Faculty",
                [
                    "Booking for the week commencing from 05 October 2026 will be accepted using the new booking portal.",
                    "Booking will be opened as usual on Wednesday, 30 September 2026 at 9:00 PM.",
                    "Until that window opens, continue creating new equipment bookings on the existing IIC Booking Portal (for earlier weeks only, as applicable).",
                    "Sign in to the new portal with Channel-i so your faculty wallet can sync (balance and legacy credit/debit history).",
                    "Confirm your department sub-wallet balance after login; recharge or transfer funds in the new portal if needed.",
                    "Ensure research scholars / students who book under you have linked (or re-linked) to your faculty wallet in the new portal.",
                    "From Wednesday, 30 September 2026 at 9:00 PM, book slots for the week commencing 05 October 2026 (and onwards) only on the new portal.",
                    "Existing bookings and historical records on the old portal remain available during the transition window.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            "IITR Faculty — New IIC Equipment Booking Portal.\n\n"
            "Booking for the week commencing from 05 October 2026 will be accepted using the new booking portal.\n"
            "Booking will be opened as usual on Wednesday, 30 September 2026 at 9:00 PM.\n\n"
            "Until that window opens, continue booking on the existing IIC Booking Portal (for earlier weeks only, as applicable).\n"
            "Please sign in to the new portal with Channel-i to sync your wallet balance and history, "
            "and ensure students are linked to your faculty wallet.\n"
            "From Wednesday, 30 September 2026 at 9:00 PM, book the week commencing 05 October 2026 "
            "(and onwards) only on the new portal.\n\n"
            f"New portal: {c['new_portal_url']}\n"
            f"Support: {c['support_email']}\n"
        )
    elif template == MigrationNotificationTemplate.STUDENT_MIGRATION:
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>This message is for <strong>IITR Students / Research Scholars</strong>. "
            "Please follow the steps below so you can book equipment when the new portal opens.</p>"
            + common_cards
            + _instructions_block(
                "Important for IITR Students",
                [
                    "Booking for the week commencing from 05 October 2026 will be accepted using the new booking portal.",
                    "Booking will be opened as usual on Wednesday, 30 September 2026 at 9:00 PM.",
                    "Until that window opens, continue creating new equipment bookings on the existing IIC Booking Portal (for earlier weeks only, as applicable).",
                    "Sign in to the new portal with Channel-i and complete your profile if prompted.",
                    "Request to link your account to your Supervisor / Faculty wallet in the new portal (required for most student bookings).",
                    "Ask your faculty supervisor to approve the wallet link request if it is pending.",
                    "From Wednesday, 30 September 2026 at 9:00 PM, book slots for the week commencing 05 October 2026 (and onwards) only on the new portal.",
                    "You can still view previous bookings and account information on the old portal during the transition window.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            "IITR Students — New IIC Equipment Booking Portal.\n\n"
            "Booking for the week commencing from 05 October 2026 will be accepted using the new booking portal.\n"
            "Booking will be opened as usual on Wednesday, 30 September 2026 at 9:00 PM.\n\n"
            "Until that window opens, continue booking on the existing IIC Booking Portal (for earlier weeks only, as applicable).\n"
            "Please sign in to the new portal with Channel-i and link your account to your "
            "Supervisor / Faculty wallet (get the link approved by your faculty).\n"
            "From Wednesday, 30 September 2026 at 9:00 PM, book the week commencing 05 October 2026 "
            "(and onwards) only on the new portal.\n\n"
            f"New portal: {c['new_portal_url']}\n"
            f"Support: {c['support_email']}\n"
        )
    elif template == MigrationNotificationTemplate.OIC_MIGRATION:
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>As Officer-in-Charge, please review legacy bookings, continue operational status management, "
            "and use the new portal for all new bookings after opening. Legacy booking slots are protected in the new portal "
            "to prevent duplicate booking during migration.</p>"
            + common_cards
            + _instructions_block(
                "Action required — OIC",
                [
                    "You may continue operational handling of eligible legacy bookings.",
                    "You may issue eligible one-time migration refunds (settlement).",
                    "You cannot create new bookings on the old portal after freeze rules apply.",
                    "All new bookings must be created on the new portal after opening.",
                    "Protected legacy slots will show as unavailable in the new portal during migration.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"OIC migration briefing. Opens: {c['migration_datetime']}.\n"
            "Operational legacy handling YES; migration refund YES; old-portal new booking NO after freeze.\n"
            f"New portal: {c['new_portal_url']}\n"
        )
    else:  # ADMIN_MIGRATION
        body = (
            hero
            + "<p style='font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:"
            + COLOR_TEXT
            + ";'>As Main Administrator you retain global visibility across departments and equipment, "
            "mapping control, migration control, and migration refund authority. New bookings must use the new portal after opening; "
            "legacy slots remain protected until released.</p>"
            + common_cards
            + _instructions_block(
                "Main Administrator",
                [
                    "Global department/equipment/mapping/block visibility remains available.",
                    "Old-portal new booking is disabled during freeze.",
                    "Migration refund authority remains available for eligible bookings.",
                    "Coordinate faculty wallet sync and student–faculty wallet linking before opening.",
                ],
            )
            + cta
            + support
        )
        text = (
            f"Dear {c['user_name']},\n\n"
            f"Main Administrator migration briefing. Opens: {c['migration_datetime']}.\n"
            f"New portal: {c['new_portal_url']}\n"
        )

    html = wrap_email_html(
        title=c["portal_name"] + " — Migration",
        subtitle="New portal opening — please read migration instructions",
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
        "new_portal_url": "https://equip.iitr.ac.in",
        "migration_datetime": "30 September 2026, 21:00 IST",
        "support_email": "iic@iitr.ac.in",
        "support_phone": "",
        "portal_name": "IIC Equipment Booking Portal",
    }
