"""
One-off announcement emails to every active IIT Roorkee faculty member (``send_announcement`` command).

Each faculty member gets an individual email (To: that member only) with an optional CC / BCC list and an optional
PDF attachment. Delivery is recorded in CommunicationLog under ``announcement:<campaign>:<user id>``, so a re-run
skips everyone already sent and retries only failures. Output carries counts, user ids and masked addresses only.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.mail import get_connection
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.utils import timezone

from iic_booking.communication.email_branding import branded_plain_footer
from iic_booking.communication.email_branding import user_display_name
from iic_booking.communication.email_branding import wrap_email_html

IITR_EMAIL_RE = re.compile(r"@(?:[a-z0-9-]+\.)*iitr\.ac\.in$", re.IGNORECASE)
LOG_KEY_PREFIX = "announcement"
LOCK_SECONDS = 6 * 60 * 60
DEFAULT_SUPPORT_EMAIL = "iicbooking@iitr.ac.in"
DEFAULT_PORTAL_URL = "https://equip.iitr.ac.in"


@dataclass(frozen=True)
class Campaign:
    slug: str
    subject: str
    title: str
    subtitle: str
    preheader: str
    template_dir: str
    attachment_name: str = ""
    signatory_name: str = ""
    signatory_title: str = "Head, Institute Instrumentation Centre"

    def attachment_path(self) -> Path | None:
        if not self.attachment_name:
            return None
        base = Path(__file__).resolve().parent.parent / "templates" / self.template_dir
        path = base / self.attachment_name
        return path if path.is_file() else None


WALLET_RECHARGE_2026_10 = Campaign(
    slug="wallet-recharge-2026-10",
    subject=(
        "Revised Wallet Recharge Procedure – IIC Equipment Booking Portal "
        "(Project Funds via SRIC Portal and Direct Recharge)"
    ),
    title="Revised Wallet Recharge Procedure",
    subtitle="Project funds through the SRIC Portal · Direct recharge when no project funds are available",
    preheader=(
        "Recharge your IIC wallet from project funds on rnd.iitr.ac.in (Ledger > New Wallet Recharge), "
        "or use Direct Cash Deposit / Bank Transfer when no project funds are available."
    ),
    template_dir="announcements/wallet_recharge_2026_10",
    attachment_name="Wallet_Recharge_Guide.pdf",
)

CAMPAIGNS: dict[str, Campaign] = {c.slug: c for c in (WALLET_RECHARGE_2026_10,)}


class AnnouncementError(Exception):
    pass


# --- Addresses -----------------------------------------------------------------------------------------------------


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def _sric_setting_tokens() -> dict[str, Callable[[], str]]:
    def field(name: str) -> Callable[[], str]:
        def read() -> str:
            from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

            return getattr(WalletSricSettings.get_singleton(), name, "") or ""

        return read

    return {
        "dean_sric": field("dean_sric_emails"),
        "ar_sric": field("ar_sric_emails"),
        "sric_office": field("recipient_emails"),
        "bill_section": field("bill_section_emails"),
    }


def parse_addresses(raw: str) -> list[str]:
    """Comma / semicolon / space separated addresses. Tokens dean_sric, ar_sric, sric_office and bill_section expand
    to the addresses saved in Wallet SRIC settings (no address is hard-coded in this public repository)."""
    tokens = _sric_setting_tokens()
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\s,;]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        expanded = (
            re.split(r"[\s,;]+", tokens[item.lower()]())
            if item.lower() in tokens
            else [item]
        )
        for addr in expanded:
            addr = addr.strip()
            if not addr:
                continue
            try:
                validate_email(addr)
            except ValidationError as exc:
                raise AnnouncementError(
                    f"Not a valid email address: {mask_email(addr)}"
                ) from exc
            if addr.lower() not in seen:
                seen.add(addr.lower())
                out.append(addr)
    return out


# --- Recipients ----------------------------------------------------------------------------------------------------


def is_iitr_email(email: str) -> bool:
    return bool(IITR_EMAIL_RE.search((email or "").strip()))


def select_faculty() -> dict[str, Any]:
    """Active IITR faculty with an @iitr.ac.in address; flagged test accounts are left out."""
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.test_accounts import is_test_user

    faculty = list(
        User.objects.filter(user_type=UserType.FACULTY)
        .select_related("department")
        .only(
            "id",
            "email",
            "name",
            "user_type",
            "is_active",
            "is_test_account",
            "department__name",
        )
        .order_by("pk"),
    )
    excluded = {
        "inactive": 0,
        "test_account": 0,
        "no_email": 0,
        "not_iitr_email": 0,
        "duplicate_email": 0,
    }
    recipients = []
    emails: set[str] = set()
    for user in faculty:
        email = (user.email or "").strip()
        if not user.is_active:
            excluded["inactive"] += 1
        elif is_test_user(user):
            excluded["test_account"] += 1
        elif not email:
            excluded["no_email"] += 1
        elif not is_iitr_email(email):
            excluded["not_iitr_email"] += 1
        elif email.lower() in emails:
            excluded["duplicate_email"] += 1
        else:
            emails.add(email.lower())
            recipients.append(user)
    return {
        "faculty_total": len(faculty),
        "excluded": excluded,
        "recipients": recipients,
    }


def log_key(campaign: Campaign, user_id: int) -> str:
    return f"{LOG_KEY_PREFIX}:{campaign.slug}:{user_id}"


def already_sent_ids(campaign: Campaign) -> set[int]:
    from iic_booking.communication.models import CommunicationLog

    prefix = f"{LOG_KEY_PREFIX}:{campaign.slug}:"
    keys = CommunicationLog.objects.filter(
        provider_message_id__startswith=prefix,
        status=CommunicationLog.CommunicationStatus.SENT,
    ).values_list("provider_message_id", flat=True)
    return {int(k[len(prefix) :]) for k in keys if k[len(prefix) :].isdigit()}


# --- Rendering -----------------------------------------------------------------------------------------------------


def _portal_url() -> str:
    url = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else DEFAULT_PORTAL_URL


def template_context(
    campaign: Campaign, *, recipient_name: str, has_attachment: bool
) -> dict[str, Any]:
    from iic_booking.users.models.sric_wallet_recharge import SRIC_PORTAL_URL
    from iic_booking.users.models.sric_wallet_recharge import SricWalletRechargeSettings
    from iic_booking.users.models.wallet_sric_settings import cashbook_match_from_date
    from iic_booking.users.sric_wallet_recharge import quiet_window_label

    portal = _portal_url()
    sric = SricWalletRechargeSettings.get_singleton()
    quiet = ""
    if sric.quiet_window_enabled and sric.quiet_window_start and sric.quiet_window_end:
        quiet = quiet_window_label(sric)["window"]
    support = (
        getattr(settings, "SUPPORT_EMAIL", "") or ""
    ).strip() or DEFAULT_SUPPORT_EMAIL
    return {
        "recipient_name": recipient_name,
        "portal_url": portal,
        "portal_host": urlparse(portal).netloc or portal,
        "wallet_url": f"{portal}/wallet",
        "sric_guide_url": f"{portal}/wallet/recharge-from-project",
        "tickets_url": f"{portal}/tickets",
        "user_guide_url": f"{portal}/user-guide",
        "sric_portal_url": SRIC_PORTAL_URL,
        "support_email": support,
        "quiet_window": quiet,
        "cashbook_from": cashbook_match_from_date().strftime("%d %B %Y").lstrip("0"),
        "has_attachment": has_attachment,
        "signatory_name": campaign.signatory_name,
        "signatory_title": campaign.signatory_title,
    }


def render(
    campaign: Campaign, *, recipient_name: str, has_attachment: bool
) -> tuple[str, str]:
    """(text, html) for one recipient."""
    ctx = template_context(
        campaign, recipient_name=recipient_name, has_attachment=has_attachment
    )
    inner = render_to_string(f"{campaign.template_dir}/email_body.html", ctx)
    text = (
        render_to_string(f"{campaign.template_dir}/email_body.txt", ctx).strip()
        + "\n"
        + branded_plain_footer()
    )
    html = wrap_email_html(
        title=campaign.title,
        subtitle=campaign.subtitle,
        preheader=campaign.preheader,
        body_inner_html=inner,
    )
    return text, html


def recipient_name_for(user) -> str:
    return user_display_name(user, fallback="Colleague")


# --- Sending -------------------------------------------------------------------------------------------------------


@dataclass
class SendOptions:
    cc: list[str]
    bcc: list[str]
    reply_to: list[str]
    attach: bool = True
    batch_size: int = 20
    pause_seconds: float = 20.0
    limit: int = 0
    max_failures: int = 10


def build_message(
    campaign: Campaign,
    *,
    to: str,
    recipient_name: str,
    options: SendOptions,
    subject_prefix: str = "",
    cc: list[str] | None = None,
    connection=None,
) -> EmailMultiAlternatives:
    path = campaign.attachment_path() if options.attach else None
    text, html = render(
        campaign, recipient_name=recipient_name, has_attachment=path is not None
    )
    message = EmailMultiAlternatives(
        subject=f"{subject_prefix}{campaign.subject}",
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
        cc=list(options.cc if cc is None else cc),
        bcc=list(options.bcc),
        reply_to=list(options.reply_to),
        connection=connection,
    )
    message.attach_alternative(html, "text/html")
    if path is not None:
        message.attach(path.name, path.read_bytes(), "application/pdf")
    return message


def _record(
    campaign: Campaign, user, *, status: str, error: str = "", cc_count: int = 0
) -> None:
    from iic_booking.communication.models import CommunicationLog

    key = log_key(campaign, user.pk)
    row = (
        CommunicationLog.objects.filter(provider_message_id=key).order_by("-pk").first()
    )
    if row is None:
        row = CommunicationLog(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient=user,
            recipient_email=user.email or "",
            subject=campaign.subject[:255],
            provider_message_id=key,
        )
    row.status = status
    row.error_message = error[:500]
    row.metadata = {
        **(row.metadata or {}),
        "campaign": campaign.slug,
        "cc_count": cc_count,
    }
    if status == CommunicationLog.CommunicationStatus.SENT:
        row.sent_at = timezone.now()
    row.save()


def send_test(
    campaign: Campaign,
    *,
    to: str,
    recipient_name: str,
    options: SendOptions,
    include_cc: bool,
) -> None:
    """One copy to ``to`` (subject prefixed [TEST]); CC only when include_cc. Nothing is written to the sent log."""
    message = build_message(
        campaign,
        to=to,
        recipient_name=recipient_name,
        options=options,
        subject_prefix="[TEST] ",
        cc=options.cc if include_cc else [],
    )
    message.bcc = list(options.bcc) if include_cc else []
    message.send(fail_silently=False)


def send_campaign(
    campaign: Campaign,
    options: SendOptions,
    *,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Send to every selected faculty member not yet sent. Returns counts and failed user ids."""
    from iic_booking.communication.models import CommunicationLog

    lock = f"{LOG_KEY_PREFIX}:{campaign.slug}:lock"
    if not cache.add(lock, "1", LOCK_SECONDS):
        raise AnnouncementError("Another run of this campaign is in progress.")
    try:
        selection = select_faculty()
        done = already_sent_ids(campaign)
        pending = [u for u in selection["recipients"] if u.pk not in done]
        if options.limit:
            pending = pending[: options.limit]
        result = {
            "selected": len(selection["recipients"]),
            "already_sent": len(done),
            "attempted": 0,
            "sent": 0,
            "failed": 0,
            "failed_user_ids": [],
            "stopped_early": False,
        }
        consecutive = 0
        batch_size = max(1, options.batch_size)
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            connection = get_connection(fail_silently=False)
            try:
                connection.open()
                for user in batch:
                    result["attempted"] += 1
                    try:
                        build_message(
                            campaign,
                            to=user.email.strip(),
                            recipient_name=recipient_name_for(user),
                            options=options,
                            connection=connection,
                        ).send(fail_silently=False)
                    except Exception as exc:  # noqa: BLE001
                        result["failed"] += 1
                        result["failed_user_ids"].append(user.pk)
                        consecutive += 1
                        _record(
                            campaign,
                            user,
                            status=CommunicationLog.CommunicationStatus.FAILED,
                            error=type(exc).__name__,
                            cc_count=len(options.cc),
                        )
                        if consecutive >= options.max_failures:
                            result["stopped_early"] = True
                            return result
                        try:
                            connection.close()
                            connection.open()
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    consecutive = 0
                    result["sent"] += 1
                    _record(
                        campaign,
                        user,
                        status=CommunicationLog.CommunicationStatus.SENT,
                        cc_count=len(options.cc),
                    )
            finally:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
            out(
                f"progress: {result['attempted']}/{len(pending)} attempted, sent={result['sent']} failed={result['failed']}"
            )
            if start + batch_size < len(pending) and options.pause_seconds > 0:
                sleep(options.pause_seconds)
        return result
    finally:
        cache.delete(lock)


def dry_run_report(
    campaign: Campaign, options: SendOptions, *, sample: int = 5
) -> dict[str, Any]:
    selection = select_faculty()
    done = already_sent_ids(campaign)
    pending = [u for u in selection["recipients"] if u.pk not in done]
    if options.limit:
        pending = pending[: options.limit]
    path = campaign.attachment_path() if options.attach else None
    batches = -(-len(pending) // max(1, options.batch_size)) if pending else 0
    return {
        "campaign": campaign.slug,
        "subject": campaign.subject,
        "faculty_total": selection["faculty_total"],
        "excluded": selection["excluded"],
        "eligible": len(selection["recipients"]),
        "already_sent": len(done),
        "to_send_now": len(pending),
        "cc": [mask_email(a) for a in options.cc],
        "bcc": [mask_email(a) for a in options.bcc],
        "reply_to": [mask_email(a) for a in options.reply_to],
        "copies_per_cc_address": len(pending) if options.cc else 0,
        "attachment": f"{path.name} ({path.stat().st_size // 1024} KB)"
        if path
        else "none",
        "batches": batches,
        "pause_seconds_between_batches": options.pause_seconds,
        "sample": [
            {
                "user_id": u.pk,
                "email": mask_email(u.email),
                "department": getattr(u.department, "name", "") or "—",
            }
            for u in pending[:sample]
        ],
    }


def write_preview(
    campaign: Campaign,
    directory: Path,
    *,
    recipient_name: str = "Prof. A. Sample",
    attach: bool = True,
) -> list[Path]:
    path = campaign.attachment_path() if attach else None
    text, html = render(
        campaign, recipient_name=recipient_name, has_attachment=path is not None
    )
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "preview.html": html,
        "preview.txt": f"Subject: {campaign.subject}\n\n{text}",
    }
    written = []
    for name, content in files.items():
        target = directory / name
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written
