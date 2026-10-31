"""Slots held for an external user while they pay.

From the moment the user chooses an online payment, the checkout's slots are held for that user for
``checkout.PAYMENT_WINDOW`` (6 minutes: 5 shown to the user plus 1 of grace) or until the payment is confirmed, whichever is earlier. While held, the
booking pipeline refuses the slots to everyone else (quote, confirm, staff bookings alike), so a payment completed in
time always finds its slots free. The hold needs no row of its own: it is the open checkout itself, and it ends when
the checkout is confirmed, cancelled or expires.
"""

from __future__ import annotations

from functools import reduce
from operator import or_
from typing import Iterable

from django.db.models import Q
from django.utils import timezone

HELD_MESSAGE = ("These slots are held for another user's payment for a few minutes. "
                "Please choose other slots or try again shortly.")


def held_slot_ids(slot_ids: Iterable[int], *, holder_user_id=None, now=None) -> set[int]:
    """The ids among ``slot_ids`` held by an open checkout of a user other than ``holder_user_id``."""
    from .models import CheckoutState, ExternalBookingCheckout

    wanted = {int(s) for s in slot_ids}
    if not wanted:
        return set()
    holds = ExternalBookingCheckout.objects.filter(
        state=CheckoutState.AWAITING_PAYMENT, expires_at__gt=now or timezone.now()
    ).filter(reduce(or_, (Q(slot_ids__contains=[s]) for s in wanted)))
    if holder_user_id is not None:
        holds = holds.exclude(user_id=holder_user_id)
    held = set()
    for ids in holds.values_list("slot_ids", flat=True):
        held.update(int(s) for s in ids or [])
    return held & wanted
