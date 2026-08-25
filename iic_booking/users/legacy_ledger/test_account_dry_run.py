"""Phase 10E — test account cleanup dry-run (zero writes)."""

from __future__ import annotations

from typing import Any

from django.apps import apps

from iic_booking.users.models import User


def test_account_cleanup_dry_run() -> dict[str, Any]:
    """
    Report test accounts and related records. is_test_account=True is the ONLY cleanup basis.
    Performs zero deletes.
    """
    test_users_qs = User.objects.filter(is_test_account=True)
    test_users = list(test_users_qs.values("id", "email", "emp_id", "user_type", "is_active")[:500])
    test_user_ids = [u["id"] for u in test_users]

    bookings = 0
    booking_ids: list[int] = []
    if apps.is_installed("equipment") and test_user_ids:
        Booking = apps.get_model("equipment", "Booking")
        booking_ids = list(Booking.objects.filter(user_id__in=test_user_ids).values_list("pk", flat=True)[:200])
        bookings = Booking.objects.filter(user_id__in=test_user_ids).count()

    wallets = 0
    sub_wallets = 0
    ledger_entries = 0
    if apps.is_installed("users") and test_user_ids:
        Wallet = apps.get_model("users", "Wallet")
        SubWallet = apps.get_model("users", "SubWallet")
        wallet_ids = list(Wallet.objects.filter(user_id__in=test_user_ids).values_list("pk", flat=True))
        wallets = len(wallet_ids)
        if wallet_ids:
            sub_wallets = SubWallet.objects.filter(wallet_id__in=wallet_ids).count()
        if apps.is_installed("users"):
            try:
                LedgerEntry = apps.get_model("users", "LedgerEntry")
                ledger_entries = LedgerEntry.objects.filter(user_id__in=test_user_ids).count()
            except LookupError:
                ledger_entries = 0

    legacy_blocks = 0
    if apps.is_installed("users"):
        try:
            LegacyBookingBlock = apps.get_model("users", "LegacyBookingBlock")
            legacy_blocks = LegacyBookingBlock.objects.filter(resolved_user_id__in=test_user_ids).count()
        except LookupError:
            legacy_blocks = 0

    return {
        "dry_run": True,
        "writes_performed": 0,
        "basis": "is_test_account=True only",
        "test_users": test_users_qs.count(),
        "test_users_sample": test_users[:50],
        "associated_bookings": bookings,
        "booking_ids_sample": booking_ids[:50],
        "wallets": wallets,
        "sub_wallets": sub_wallets,
        "ledger_entries": ledger_entries,
        "legacy_blocks_linked": legacy_blocks,
        "runbook": [
            "dry-run",
            "operator review",
            "explicit cleanup approval",
            "cleanup execution (separate authorized phase)",
        ],
        "note": "Phase 10E does not delete test accounts on production.",
    }
