"""Portal migration: Employee-ID wallet mapping, immutable legacy ledger, cutover state."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class PortalMigrationPhase(models.TextChoices):
    PREPARATION = "PREPARATION", _("Preparation")
    PARALLEL_OPERATION = "PARALLEL_OPERATION", _("Parallel operation")
    FINANCIAL_FREEZE = "FINANCIAL_FREEZE", _("Financial freeze")
    FINAL_SYNC = "FINAL_SYNC", _("Final sync")
    RECONCILIATION = "RECONCILIATION", _("Reconciliation")
    NEW_PORTAL_ACTIVE = "NEW_PORTAL_ACTIVE", _("New portal active")
    OLD_PORTAL_READ_ONLY = "OLD_PORTAL_READ_ONLY", _("Old portal read-only")
    OLD_PORTAL_REDIRECT = "OLD_PORTAL_REDIRECT", _("Old portal redirect")
    ARCHIVED = "ARCHIVED", _("Archived")


class LegacyWalletMappingStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    VALID = "VALID", _("Valid")
    MAPPED = "MAPPED", _("Mapped")
    IMPORTED = "IMPORTED", _("Imported")
    RECONCILED = "RECONCILED", _("Reconciled")
    MISMATCH = "MISMATCH", _("Mismatch")
    EXCEPTION = "WALLET_MAPPING_EXCEPTION", _("Wallet mapping exception")
    MISSING_EMPLOYEE_ID = "MISSING_EMPLOYEE_ID", _("Missing employee ID")
    DUPLICATE_EMPLOYEE_ID = "DUPLICATE_EMPLOYEE_ID", _("Duplicate employee ID")
    CHANNEL_I_NOT_FOUND = "CHANNEL_I_NOT_FOUND", _("Channel-I / new user not found")
    MULTIPLE_MATCH = "MULTIPLE_MATCH", _("Multiple matches")
    MANUAL_REVIEW = "MANUAL_REVIEW", _("Manual review")


class LegacyLedgerDirection(models.TextChoices):
    CREDIT = "CREDIT", _("Credit")
    DEBIT = "DEBIT", _("Debit")


class PortalMigrationState(models.Model):
    """Singleton cutover + booking-lock settings. Do not put secrets here."""

    singleton_key = models.CharField(max_length=16, unique=True, default="default")
    phase = models.CharField(
        max_length=32,
        choices=PortalMigrationPhase.choices,
        default=PortalMigrationPhase.PREPARATION,
    )
    # Default True so existing production booking on equip.iitr.ac.in is not bricked.
    # Set False for the official parallel week.
    end_user_booking_enabled = models.BooleanField(default=True)
    booking_opens_at = models.DateTimeField(null=True, blank=True)
    booking_lock_message = models.TextField(
        blank=True,
        default=(
            "New IIC Equipment Booking Portal\n\n"
            "The new portal is currently being prepared for launch.\n\n"
            "Online equipment booking will be available from:\n\n"
            "    {date}\n"
            "    {time}\n\n"
            "Until then, please continue using the existing IIC Booking Portal.\n\n"
            "Your wallet migration is being synchronized and your wallet "
            "balance and transaction history will remain available."
        ),
    )
    last_wallet_txn_watermark = models.PositiveBigIntegerField(default=0)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(blank=True, default="")
    last_sync_batch = models.CharField(max_length=64, blank=True, default="")
    last_sync_duration_ms = models.PositiveIntegerField(default=0)
    last_sync_imported_count = models.PositiveIntegerField(default=0)
    last_sync_processed_count = models.PositiveIntegerField(default=0)
    incremental_sync_enabled = models.BooleanField(default=False)
    legacy_ledger_frozen = models.BooleanField(default=False)
    sync_runs_total = models.PositiveIntegerField(default=0)
    sync_failures_total = models.PositiveIntegerField(default=0)
    transactions_imported_total = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Portal migration state")
        verbose_name_plural = _("Portal migration state")

    def __str__(self) -> str:
        return f"PortalMigrationState {self.phase}"

    @classmethod
    def get_solo(cls) -> "PortalMigrationState":
        obj, _ = cls.objects.get_or_create(singleton_key="default")
        return obj


class LegacyWalletAccountMapping(models.Model):
    """Employee-ID link between old portal wallet owner and new Channel-I user."""

    employee_id = models.CharField(max_length=50, unique=True, db_index=True)
    old_user_id = models.IntegerField(null=True, blank=True)
    old_wallet_id = models.IntegerField(null=True, blank=True)
    old_name = models.CharField(max_length=255, blank=True, default="")
    old_email = models.CharField(max_length=255, blank=True, default="")
    new_user = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_wallet_mappings",
    )
    channel_i_employee_id = models.CharField(max_length=50, blank=True, default="")
    channel_i_email = models.CharField(max_length=255, blank=True, default="")
    channel_i_name = models.CharField(max_length=255, blank=True, default="")
    mapping_status = models.CharField(
        max_length=40,
        choices=LegacyWalletMappingStatus.choices,
        default=LegacyWalletMappingStatus.PENDING,
        db_index=True,
    )
    reconciliation_status = models.CharField(max_length=32, blank=True, default="")
    exception_reason = models.TextField(blank=True, default="")
    old_credits = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    old_debits = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    old_wallet_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    imported_credits = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    imported_debits = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    migration_batch = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Legacy wallet account mapping")
        verbose_name_plural = _("Legacy wallet account mappings")
        indexes = [
            models.Index(fields=["mapping_status", "employee_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.employee_id} ({self.mapping_status})"


class LegacyWalletLedgerEntry(models.Model):
    """Immutable copy of old-portal wallet_transactions. Never update financial columns."""

    SOURCE_OLD_PORTAL = "OLD_PORTAL"

    mapping = models.ForeignKey(
        LegacyWalletAccountMapping,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    employee_id = models.CharField(max_length=50, db_index=True)
    source_system = models.CharField(max_length=32, default=SOURCE_OLD_PORTAL)
    source_transaction_id = models.PositiveBigIntegerField()
    source_wallet_id = models.IntegerField(null=True, blank=True)
    source_user_id = models.IntegerField()
    occurred_at = models.DateTimeField()
    direction = models.CharField(max_length=8, choices=LegacyLedgerDirection.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    running_balance_source = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True, default="")
    reference = models.CharField(max_length=255, blank=True, default="")
    utr = models.CharField(max_length=64, blank=True, default="")
    migration_batch = models.CharField(max_length=64, db_index=True)
    checksum = models.CharField(max_length=64)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Legacy wallet ledger entry")
        verbose_name_plural = _("Legacy wallet ledger entries")
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "source_transaction_id"],
                name="uniq_legacy_wallet_source_txn",
            )
        ]
        indexes = [
            models.Index(fields=["employee_id", "occurred_at"]),
            models.Index(fields=["source_transaction_id"]),
        ]
        ordering = ["-occurred_at", "-source_transaction_id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Legacy wallet ledger entries are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Legacy wallet ledger entries cannot be deleted")

    def __str__(self) -> str:
        return f"{self.source_system}:{self.source_transaction_id}"


class LegacyWalletSyncDeadLetter(models.Model):
    """Rows that could not be imported (missing/duplicate emp_id, etc.)."""

    source_transaction_id = models.PositiveBigIntegerField(unique=True)
    source_user_id = models.IntegerField(null=True, blank=True)
    employee_id = models.CharField(max_length=50, blank=True, default="")
    reason = models.CharField(max_length=64)
    detail = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Legacy wallet sync dead letter")
        verbose_name_plural = _("Legacy wallet sync dead letters")


class PortalMigrationPhaseTransition(models.Model):
    """Auditable explicit phase change. Never inferred from booleans."""

    from_phase = models.CharField(max_length=32)
    to_phase = models.CharField(max_length=32)
    actor_email = models.CharField(max_length=255, blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Portal migration phase transition")
        verbose_name_plural = _("Portal migration phase transitions")

    def __str__(self) -> str:
        return f"{self.from_phase} -> {self.to_phase}"


class LegacyBookingHistoryRecord(models.Model):
    """Separate historical booking copy. Never used for slots, waitlist, or billing.

    Old booking IDs are stored only here. They must never be written to equipment.Booking.
    This table is empty until an operator explicitly archives history; no auto-import.
    """

    source_booking_id = models.PositiveBigIntegerField()
    employee_id = models.CharField(max_length=50, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    historical_label = models.CharField(max_length=32, default="Historical / Legacy")

    class Meta:
        verbose_name = _("Legacy booking history record")
        verbose_name_plural = _("Legacy booking history records")
        constraints = [
            models.UniqueConstraint(
                fields=["source_booking_id"],
                name="uniq_legacy_booking_source_id",
            )
        ]
