"""Celery tasks for the users app (e.g. wallet low balance alerts)."""

import logging
from decimal import Decimal

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

@shared_task(name="users.delete_unverified_user_after_verification_expiry")
def delete_unverified_user_after_verification_expiry(user_id: int, sent_at_iso: str | None = None) -> bool:
    """
    Delete a newly registered user if they didn't act on the verification email within 10 minutes.

    - Only deletes if user is still not email_verified.
    - Uses verification_email_sent_at to avoid deleting if user requested a newer verification email.

    Args:
        user_id: User PK to check.
        sent_at_iso: ISO timestamp captured when email was sent; if provided and doesn't match the user's
            current verification_email_sent_at, task is ignored (a newer email was sent).

    Returns:
        True if the user was deleted, False otherwise.
    """
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from iic_booking.users.models import User

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return False

    if user.email_verified:
        return False

    if sent_at_iso:
        try:
            sent_at = parse_datetime(sent_at_iso)
        except Exception:
            sent_at = None
        if sent_at and timezone.is_naive(sent_at):
            sent_at = timezone.make_aware(sent_at, timezone.get_current_timezone())
        current = user.verification_email_sent_at
        if current and sent_at and abs((current - sent_at).total_seconds()) > 1:
            # A newer verification email was sent; don't delete based on older one.
            return False

    # If no timestamp stored, fall back to date_joined.
    start = user.verification_email_sent_at or user.date_joined
    if not start:
        return False

    if timezone.now() - start < timezone.timedelta(minutes=10):
        return False

    # Deemed rejected: delete user and related uploads.
    user.delete()
    return True


@shared_task(name="users.send_wallet_low_balance_alerts")
def send_wallet_low_balance_alerts() -> int:
    """
    Run daily at 11:00 AM. For each user who has wallet low balance alert enabled
    and has a wallet, if current total balance < threshold, send an email to the
    Supervisor. For shared wallets (e.g. faculty wallet used by students), the
    Supervisor is the one who receives the alert.

    Returns:
        Number of alert emails sent.
    """
    from iic_booking.users.models import User
    from iic_booking.users.models import Wallet
    from iic_booking.communication.service import CommunicationService

    users_to_check = User.objects.filter(
        wallet_low_balance_alert_enabled=True,
        wallet_low_balance_alert_threshold__isnull=False,
        wallet_low_balance_alert_threshold__gt=0,
    ).select_related("wallet")

    sent = 0
    for user in users_to_check:
        try:
            try:
                wallet = user.wallet
            except Wallet.DoesNotExist:
                continue
            balance = wallet.total_balance
            threshold = user.wallet_low_balance_alert_threshold
            if threshold is None or balance is None:
                continue
            if Decimal(str(balance)) >= Decimal(str(threshold)):
                continue
            link = f"{getattr(settings, 'FRONTEND_URL', '')}/wallet"
            context = {
                "user_name": user.name or user.email or "User",
                "user_email": user.email or "",
                "balance": f"{balance:.2f}",
                "threshold": f"{threshold:.2f}",
                "link": link,
            }
            CommunicationService.send_email(
                recipient=user,
                template="wallet_low_balance_email",
                template_context=context,
                created_by=None,
            )
            sent += 1
            logger.info("Wallet low balance alert sent to %s (balance=%s, threshold=%s)", user.email, balance, threshold)
        except Exception as e:
            logger.exception("Failed to send wallet low balance alert to user %s: %s", getattr(user, "id"), e)

    logger.info("send_wallet_low_balance_alerts: sent=%d", sent)
    return sent


@shared_task(name="users.expire_wallet_credit_facilities")
def expire_wallet_credit_facilities() -> int:
    """
    Periodic task (django-celery-beat): end faculty recharge credit windows that passed
    without parse credit; notify faculty. Prefer scheduling hourly or every few hours.

    Returns:
        Number of requests moved to expired_unpaid in this run.
    """
    from iic_booking.users.wallet_credit_facility import expire_due_wallet_credit_facilities

    n = expire_due_wallet_credit_facilities()
    logger.info("expire_wallet_credit_facilities: expired_count=%s", n)
    return n


@shared_task(name="users.wallet_credit_facility_v2_overdue_and_reminders")
def wallet_credit_facility_v2_overdue_and_reminders() -> dict:
    """Mark overdue invoices and emit reminder audit events for unpaid credit facilities.

    Interval is driven by WalletCreditPolicy.overdue_reminder_interval_days /
    reminder_days_before_due (not hardcoded aggression). Schedule daily via beat.
    """
    from datetime import timedelta

    from django.utils import timezone
    from iic_booking.users.models.wallet_credit_facility import (
        WalletCreditFacility,
        WalletCreditFacilityStatus,
        WalletCreditPolicy,
    )
    from iic_booking.users.wallet_credit_facility_v2 import audit, mark_overdue_facilities

    overdue_n = mark_overdue_facilities()
    policy = WalletCreditPolicy.get_solo()
    today = timezone.localdate()
    before = int(policy.reminder_days_before_due or 0)
    reminded = 0
    if before > 0:
        target = today + timedelta(days=before)
        qs = WalletCreditFacility.objects.filter(
            status__in={
                WalletCreditFacilityStatus.CREDITED,
                WalletCreditFacilityStatus.PARTIALLY_SETTLED,
            },
            due_date=target,
            outstanding_amount__gt=0,
        )
        for facility in qs:
            audit(
                facility,
                actor=None,
                action="REMINDER_BEFORE_DUE",
                new=str(facility.outstanding_amount),
                metadata={"due_date": str(facility.due_date)},
            )
            reminded += 1
    logger.info(
        "wallet_credit_facility_v2_overdue_and_reminders: overdue=%s reminded=%s",
        overdue_n,
        reminded,
    )
    return {"overdue_marked": overdue_n, "reminders": reminded}

@shared_task(name="users.sync_legacy_wallet_ledger")
def sync_legacy_wallet_ledger() -> dict:
    """Incremental copy of old wallet_transactions into the immutable legacy ledger.

    Schedule via django-celery-beat using PORTAL_MIGRATION_SYNC_INTERVAL_SECONDS as guidance.
    Does not modify already imported rows. Credentials are environment-only.
    """
    from django.utils import timezone
    from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader
    from iic_booking.users.legacy_ledger.sync import run_ledger_sync
    from iic_booking.users.models.portal_migration import PortalMigrationState

    state = PortalMigrationState.get_solo()
    if state.legacy_ledger_frozen:
        return {"ok": True, "skipped": "frozen"}
    if not state.incremental_sync_enabled:
        return {"ok": True, "skipped": "incremental_sync_disabled"}
    if state.phase not in (
        "PARALLEL_OPERATION",
        "FINANCIAL_FREEZE",
        "FINAL_SYNC",
        "RECONCILIATION",
    ):
        return {"ok": True, "skipped": f"phase_{state.phase}"}
    batch = timezone.now().strftime("sync-%Y%m%d%H%M%S")
    try:
        with OldMySQLReader() as reader:
            result = run_ledger_sync(reader, batch=batch, dry_run=False)
    except OldMySQLNotConfigured:
        logger.warning("sync_legacy_wallet_ledger skipped: OLD_MYSQL_* not configured")
        return {"ok": False, "skipped": "not_configured"}
    except Exception:
        logger.exception("sync_legacy_wallet_ledger failed")
        raise
    return result


@shared_task(name="users.expire_channel_i_students")
def expire_channel_i_students() -> int:
    """Idempotent student expiry. No-op unless STUDENT_LIFECYCLE_ENABLED."""
    from iic_booking.users.identity.lifecycle import expire_due_students

    n = expire_due_students()
    logger.info("expire_channel_i_students: disabled_count=%s", n)
    return n
