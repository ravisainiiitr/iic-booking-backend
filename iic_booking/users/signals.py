"""Signals for user-related models."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

# Wallet transactions removed; all transactions are SubWalletTransaction.
# Add post_save for SubWalletTransaction here if notification is needed.

logger = logging.getLogger(__name__)

LIFECYCLE_USER_FIELDS = frozenset(
    {
        "user_type",
        "user_type_alias",
        "degree_name",
        "joining_date",
        "graduation_date",
        "program_start_date",
        "program_end_date",
    }
)
LIFECYCLE_PROFILE_FIELDS = frozenset({"student_degree_name", "student_start_date", "student_end_date"})


def _refresh_lifecycle(user) -> None:
    from iic_booking.users import account_lifecycle
    from iic_booking.users.models import AccountLifecycle

    try:
        account_lifecycle.forget_cached(user)
        if account_lifecycle.category_for(user) or account_lifecycle._safe(
            lambda: AccountLifecycle.objects.filter(user_id=user.pk).exists(), False
        ):
            account_lifecycle.refresh(user)
        account_lifecycle.forget_cached(user)
    except Exception:
        logger.exception("account lifecycle refresh failed user=%s", getattr(user, "pk", None))


@receiver(post_save, sender="users.User", dispatch_uid="account_lifecycle_user_saved")
def account_lifecycle_user_saved(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """Keep the stored programme / employment end date in step with the profile dates and degree."""
    if raw or created:
        return
    if update_fields is not None and not (set(update_fields) & LIFECYCLE_USER_FIELDS):
        return
    _refresh_lifecycle(instance)


@receiver(post_save, sender="users.ChannelIIdentityProfile", dispatch_uid="account_lifecycle_profile_saved")
def account_lifecycle_profile_saved(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    if update_fields is not None and not (set(update_fields) & LIFECYCLE_PROFILE_FIELDS):
        return
    user = getattr(instance, "user", None)
    if user is not None:
        _refresh_lifecycle(user)
