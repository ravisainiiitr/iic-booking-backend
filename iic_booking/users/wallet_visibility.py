"""Who sees which transactions of a wallet.

* The wallet owner sees every transaction.
* An internal IIT Roorkee student booking against a supervisor's (shared) wallet sees every transaction of that
  wallet too, exactly as the owner does (12 Nov 2026), so the research group can see what the wallet was spent on.
* Anyone else using a wallet that is not their own (for example an "Other" user on a faculty wallet) sees the
  debits made for them plus every credit, as before.

Every place that lists a wallet's transactions to its users (Wallet page, sub-wallet history, Booking Assistant)
starts from ``visible_wallet_transactions``.
"""

from __future__ import annotations

from django.db.models import Q
from django.db.models import QuerySet


def sees_all_wallet_transactions(user, wallet) -> bool:
    if wallet is None or user is None:
        return False
    if wallet.user_id == getattr(user, "pk", None):
        return True
    from iic_booking.equipment.results_sharing_service import INTERNAL_STUDENT_TYPES

    # Signing in already requires an active account, so the account type decides.
    return getattr(user, "user_type", None) in INTERNAL_STUDENT_TYPES


def visible_wallet_transactions(user, wallet, qs: QuerySet) -> QuerySet:
    """Narrow ``qs`` (SubWalletTransaction rows of ``wallet``) to what ``user`` may see."""
    if sees_all_wallet_transactions(user, wallet):
        return qs
    from iic_booking.users.models import SubWalletTransaction

    return qs.filter(Q(related_user_id=user.pk) | Q(transaction_type=SubWalletTransaction.TransactionType.CREDIT))
