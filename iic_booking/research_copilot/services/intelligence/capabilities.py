"""
What the signed-in user may do, for choosing which Copilot actions to offer.

Every check reuses the portal's own rule and is computed lazily (a wallet question never queries
bookings or My Research). Hiding an action is only a courtesy: the handler and the portal endpoint
still enforce authorization on every call.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any
from iic_booking.users.display import get_user_display_name


class Capabilities:
    def __init__(self, user):
        self.user = user

    # ------------------------------------------------------------------ identity
    @cached_property
    def user_type(self) -> str:
        return str(getattr(self.user, "user_type", "") or "")

    @cached_property
    def is_student(self) -> bool:
        from iic_booking.users.models.user_type import UserType

        return self.user_type == UserType.STUDENT

    @cached_property
    def is_faculty(self) -> bool:
        from iic_booking.users.models.user_type import UserType

        return self.user_type == UserType.FACULTY

    # ------------------------------------------------------------------ wallet
    @cached_property
    def wallet(self) -> Any:
        user = self.user
        try:
            wallet = user.get_accessible_wallet() if hasattr(user, "get_accessible_wallet") else None
            if wallet is None:
                from iic_booking.users.models import Wallet

                wallet = Wallet.objects.filter(user=user).first()
        except Exception:  # noqa: BLE001
            wallet = None
        return wallet

    @cached_property
    def can_own_wallet(self) -> bool:
        try:
            return bool(self.user.can_have_wallet())
        except Exception:  # noqa: BLE001
            return False

    @cached_property
    def has_wallet(self) -> bool:
        return self.wallet is not None or self.can_own_wallet

    @cached_property
    def wallet_is_shared(self) -> bool:
        return self.wallet is not None and self.wallet.user_id != self.user.pk

    @cached_property
    def wallet_owner_name(self) -> str:
        owner = getattr(self.wallet, "user", None) if self.wallet_is_shared else None
        return (get_user_display_name(owner) or "") if owner else ""

    @cached_property
    def can_recharge(self) -> bool:
        """Same rule as the Wallet page: own wallet, or an IITR student whose department allows student recharge."""
        if not self.has_wallet:
            return False
        if not self.wallet_is_shared:
            return True
        if not self.is_student:
            return False
        try:
            from iic_booking.users.student_wallet_recharge import student_has_any_recharge_department

            return bool(student_has_any_recharge_department(self.user))
        except Exception:  # noqa: BLE001
            return False

    @cached_property
    def credit_summary(self) -> dict[str, Any]:
        try:
            from iic_booking.research_copilot.services.v2.mutations import domain_bridge

            code, data = domain_bridge.call_wallet_credit_summary(user=self.user)
        except Exception:  # noqa: BLE001
            return {}
        return data if code < 400 and isinstance(data, dict) else {}

    @cached_property
    def credit_eligibility(self) -> dict[str, Any]:
        return dict(self.credit_summary.get("eligibility") or {})

    @cached_property
    def credit_can_request(self) -> bool:
        return bool(self.credit_summary.get("feature_enabled")) and bool(self.credit_eligibility.get("allowed"))

    @cached_property
    def credit_has_facility(self) -> bool:
        return bool(self.credit_summary.get("active_facility_reference"))

    @cached_property
    def credit_visible(self) -> bool:
        return bool(self.credit_summary.get("feature_enabled")) and (self.credit_can_request or self.credit_has_facility)

    # ------------------------------------------------------------------ bookings
    @cached_property
    def upcoming_bookings(self) -> list[dict[str, Any]]:
        from iic_booking.research_copilot.services.intelligence import booking_changes

        try:
            return booking_changes.cancellable_bookings(self.user)
        except Exception:  # noqa: BLE001
            return []

    @cached_property
    def has_upcoming_bookings(self) -> bool:
        return bool(self.upcoming_bookings)

    @cached_property
    def has_self_changeable_bookings(self) -> bool:
        """Upcoming bookings the user may still cancel / reschedule themselves (lab has not accepted the sample)."""
        from iic_booking.research_copilot.services.intelligence import booking_changes

        if not self.upcoming_bookings:
            return False
        try:
            return bool(booking_changes.cancellable_bookings(self.user, for_cancel=True))
        except Exception:  # noqa: BLE001
            return self.has_upcoming_bookings

    # ------------------------------------------------------------------ My Research
    @cached_property
    def my_research_available(self) -> bool:
        try:
            from iic_booking.my_research.access import feature_enabled, is_eligible

            return feature_enabled() and is_eligible(self.user)
        except Exception:  # noqa: BLE001
            return False

    @cached_property
    def can_create_workspace(self) -> bool:
        try:
            from iic_booking.my_research.access import can_create_workspace

            return bool(can_create_workspace(self.user))
        except Exception:  # noqa: BLE001
            return False

    @cached_property
    def groups_available(self) -> bool:
        try:
            from iic_booking.my_research.access import is_eligible
            from iic_booking.my_research.group_access import groups_enabled

            return groups_enabled() and is_eligible(self.user)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ helpers
    def allows(self, name: str | None) -> bool:
        if not name:
            return True
        negate = name.startswith("!")
        value = bool(getattr(self, name.lstrip("!")))
        return not value if negate else value
