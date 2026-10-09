"""Singleton settings: SRIC Office recipient emails for faculty wallet recharge notifications."""

from datetime import date

from django.db import models
from django.utils.translation import gettext_lazy as _

PORTAL_LAUNCH_DATE = date(2026, 9, 30)


class WalletSricSettings(models.Model):
    """
    Single-row configuration for SRIC Office email recipients.
    Admins edit this in Django admin; faculty wallet recharge flow uses it when sending to SRIC.
    """

    recipient_emails = models.TextField(
        _("SRIC Office email addresses"),
        blank=True,
        help_text=_(
            "One address per line, or comma/semicolon separated. "
            "Used when a faculty member sends a Project Grant wallet recharge request to the SRIC Office."
        ),
    )
    bill_section_emails = models.TextField(
        _("SRIC Bill Section email addresses"),
        blank=True,
        help_text=_(
            "One address per line, or comma/semicolon separated. "
            "Used for Direct Cash Deposit / Bank Transfer wallet recharge requests. "
            "Configurable by Main Administrator and Department Administrator."
        ),
    )
    project_grant_recharge_enabled = models.BooleanField(
        _("Allow wallet recharge requests via Project Grant"),
        default=False,
        db_default=False,
        help_text=_(
            "When off, faculty cannot raise new Project Grant recharge requests (or send unsent ones to the "
            "SRIC Office). Direct Cash Deposit / Bank Transfer is unaffected."
        ),
    )
    direct_cash_recharge_enabled = models.BooleanField(
        _("Allow wallet recharge via Direct Cash Deposit / Bank Transfer"),
        default=True,
        db_default=True,
        help_text=_("When off, users cannot raise new Direct Cash Deposit / Bank Transfer recharge requests."),
    )
    online_gateway_recharge_enabled = models.BooleanField(
        _("Allow wallet recharge via online payment gateway"),
        default=False,
        db_default=False,
        help_text=_("When on, users can recharge a department sub-wallet instantly through Razorpay."),
    )
    peer_transfer_enabled = models.BooleanField(
        _("Allow wallet transfers within the same department"),
        default=True,
        db_default=True,
        help_text=_("When off, faculty cannot start new wallet-to-wallet transfers."),
    )
    project_grant_cc_emails = models.TextField(
        _("Project Grant recharge CC email addresses"),
        blank=True,
        help_text=_(
            "Additional addresses copied on Project Grant recharge requests. "
            "The requesting user is always copied. CC recipients get the request details "
            "without Approve / Decline links."
        ),
    )
    cash_deposit_cc_emails = models.TextField(
        _("Direct Cash / Bank Transfer recharge CC email addresses"),
        blank=True,
        help_text=_(
            "Additional addresses copied on Direct Cash Deposit / Bank Transfer recharge requests. "
            "The requesting user is always copied. CC recipients get the request details "
            "without Approve / Decline links."
        ),
    )
    grant_code_for_credit = models.CharField(
        _("Default grant code (fallback)"),
        max_length=80,
        default="IIC-000-002",
        help_text=_(
            "Used in the SRIC Office recharge email only when the selected internal department "
            "has no grant code of its own. Prefer setting codes per department below / on this page."
        ),
    )
    ar_sric_emails = models.TextField(
        _("AR SRIC email addresses"),
        blank=True,
        help_text=_(
            "Copied (without Approve / Decline links) on every Project Grant and Direct Cash Deposit "
            "recharge request and on its final decision."
        ),
    )
    dean_sric_emails = models.TextField(
        _("Dean SRIC email addresses"),
        blank=True,
        help_text=_(
            "Copied (without Approve / Decline links) on every Project Grant recharge request "
            "and on its final decision."
        ),
    )
    decline_converts_to_credit = models.BooleanField(
        _("Treat SRIC-declined Project Grant requests as auto-approved credit"),
        default=True,
        help_text=_(
            "When SRIC declines a Project Grant request (before or after approval), the request is "
            "cancelled and the amount is treated as an auto-approved credit facility, recovered from "
            "the faculty member's next approved recharge for the same department."
        ),
    )
    auto_read_cashbook_mailbox = models.BooleanField(
        _("Read the SRIC cash-book mailbox automatically"),
        default=True,
        help_text=_(
            "Every 30 minutes, read new cash-book emails from the configured senders (IMAP_* server "
            "settings) and mark matching recharge requests as fund-received."
        ),
    )
    cashbook_sender_emails = models.TextField(
        _("SRIC cash-book sender addresses"),
        blank=True,
        default="bills@sric.iitr.ac.in",
        help_text=_("Only emails from these senders are read by the automatic cash-book reader."),
    )
    fund_receipt_overdue_days = models.PositiveSmallIntegerField(
        _("Flag approved requests without a cash-book match after (days)"),
        default=15,
        help_text=_(
            "Approved recharge requests with no matching SRIC cash-book entry after this many days are "
            "shown to the Main Administrator and Account In-charge every time they open the dashboard."
        ),
    )
    cashbook_match_from_date = models.DateField(
        _("Match cash-book entries dated on or after"),
        default=PORTAL_LAUNCH_DATE,
        help_text=_(
            "Only SRIC cash-book entries whose own date is on or after this date are used to match wallet "
            "recharge requests (portal launch). Older or undated entries are ignored."
        ),
    )

    class Meta:
        db_table = "users_walletsricsettings"
        verbose_name = _("Wallet SRIC office notification settings")
        verbose_name_plural = _("Wallet SRIC office notification settings")

    def __str__(self) -> str:
        return "Wallet SRIC office emails"

    @classmethod
    def get_singleton(cls) -> "WalletSricSettings":
        # Bill Section routing is configured by the Main Admin; when empty, recharge mail falls back to the
        # SRIC Office recipients and then ACCOUNTS_EMAIL (see get_sric_bill_section_emails).
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={"recipient_emails": "", "bill_section_emails": "", "grant_code_for_credit": "IIC-000-002"},
        )
        return obj


def cashbook_match_from_date() -> date:
    return WalletSricSettings.get_singleton().cashbook_match_from_date or PORTAL_LAUNCH_DATE


def project_grant_switch_exempt(user) -> bool:
    """Test faculty accounts may always use Project Grant recharge, whatever the admin switch says."""
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.test_accounts import is_test_user

    return (
        user is not None
        and str(getattr(user, "user_type", "") or "") == UserType.FACULTY
        and is_test_user(user)
    )


def _department_allows(option: str, department) -> bool:
    """Master is on: does the department (of the sub-wallet involved) still allow the option?"""
    if department is None:
        return True
    from iic_booking.users.wallet_payment_modes import department_allows

    return department_allows(option, department)


def project_grant_recharge_enabled(user=None, department=None) -> bool:
    if project_grant_switch_exempt(user):
        return True
    if not WalletSricSettings.get_singleton().project_grant_recharge_enabled:
        return False
    return _department_allows("project_grant", department)


def direct_cash_recharge_enabled(department=None) -> bool:
    if not WalletSricSettings.get_singleton().direct_cash_recharge_enabled:
        return False
    return _department_allows("direct_cash", department)


def online_gateway_recharge_enabled(department=None) -> bool:
    if not WalletSricSettings.get_singleton().online_gateway_recharge_enabled:
        return False
    return _department_allows("online_gateway", department)


def peer_transfer_enabled(department=None) -> bool:
    if not WalletSricSettings.get_singleton().peer_transfer_enabled:
        return False
    return _department_allows("peer_transfer", department)


AWAITING_APPROVAL_MESSAGE = "Awaiting Competent Authority Approval."


def wallet_mode_flags(user=None) -> dict:
    """Wallet funding / transfer options the Main Administrator can switch on or off, as seen by ``user``.

    The top-level flags are the masters; ``department_modes`` lists departments that switch an
    enabled master off (``{department_id: {flag: False}}``).
    """
    from iic_booking.users.wallet_credit_facility_v2 import feature_enabled as credit_feature_enabled
    from iic_booking.users.wallet_payment_modes import user_department_modes

    s = WalletSricSettings.get_singleton()
    exempt = project_grant_switch_exempt(user)
    masters = {
        "project_grant": bool(s.project_grant_recharge_enabled),
        "direct_cash": bool(s.direct_cash_recharge_enabled),
        "online_gateway": bool(s.online_gateway_recharge_enabled),
        "peer_transfer": bool(s.peer_transfer_enabled),
    }
    return {
        "project_grant_recharge_enabled": masters["project_grant"] or exempt,
        "direct_cash_recharge_enabled": masters["direct_cash"],
        "online_gateway_recharge_enabled": masters["online_gateway"],
        "peer_transfer_enabled": masters["peer_transfer"],
        "credit_facility_enabled": bool(credit_feature_enabled()),
        "department_modes": user_department_modes(masters, project_grant_exempt=exempt),
        "disabled_message": AWAITING_APPROVAL_MESSAGE,
    }


class WalletCashbookMailboxMessage(models.Model):
    """One SRIC cash-book email already handled by the automatic mailbox reader."""

    folder = models.CharField(_("Folder"), max_length=120)
    uid = models.CharField(_("IMAP UID"), max_length=32)
    subject = models.CharField(_("Subject"), max_length=500, blank=True)
    from_addr = models.CharField(_("From"), max_length=255, blank=True)
    attachment_name = models.CharField(_("Attachment"), max_length=255, blank=True)
    rows_parsed = models.PositiveIntegerField(_("Rows parsed"), default=0)
    rows_stored = models.PositiveIntegerField(_("Rows stored"), default=0)
    error = models.TextField(_("Error"), blank=True)
    processed_at = models.DateTimeField(_("Processed at"), auto_now_add=True)

    class Meta:
        verbose_name = _("SRIC cash-book mailbox message")
        verbose_name_plural = _("SRIC cash-book mailbox messages")
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(fields=["folder", "uid"], name="unique_cashbook_mailbox_folder_uid"),
        ]

    def __str__(self) -> str:
        return f"{self.folder}#{self.uid} ({self.rows_stored} rows)"
