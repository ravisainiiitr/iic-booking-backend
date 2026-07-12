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
