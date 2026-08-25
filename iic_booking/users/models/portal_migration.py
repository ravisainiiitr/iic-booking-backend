"""Portal migration: Employee-ID wallet mapping, immutable legacy ledger, cutover state."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class PortalMigrationPhase(models.TextChoices):
    """Authoritative cutover phases.

    Operator-facing aliases (docs):
      LEGACY_ACTIVE / MIGRATION_PREPARATION → PREPARATION or PARALLEL_OPERATION
      MIGRATION_READY → PARALLEL_OPERATION (sync on, booking optionally locked)
      MIGRATION_RUNNING → FINAL_SYNC
      MIGRATION_VERIFICATION → RECONCILIATION
      MIGRATION_INTERRUPTED → stored via interrupted flag + last phase
      LEGACY_REDIRECT → OLD_PORTAL_REDIRECT
    """

    PREPARATION = "PREPARATION", _("Preparation")
    PARALLEL_OPERATION = "PARALLEL_OPERATION", _("Parallel operation")
    FINANCIAL_FREEZE = "FINANCIAL_FREEZE", _("Financial freeze")
    FINAL_SYNC = "FINAL_SYNC", _("Final sync")
    RECONCILIATION = "RECONCILIATION", _("Reconciliation")
    NEW_PORTAL_ACTIVE = "NEW_PORTAL_ACTIVE", _("New portal active")
    OLD_PORTAL_READ_ONLY = "OLD_PORTAL_READ_ONLY", _("Old portal read-only")
    OLD_PORTAL_REDIRECT = "OLD_PORTAL_REDIRECT", _("Old portal redirect")
    ARCHIVED = "ARCHIVED", _("Archived")
    MIGRATION_INTERRUPTED = "MIGRATION_INTERRUPTED", _("Migration interrupted")


# Doc ↔ DB phase aliases (read-side convenience; writes use PortalMigrationPhase).
PORTAL_MIGRATION_PHASE_ALIASES = {
    "LEGACY_ACTIVE": PortalMigrationPhase.PREPARATION,
    "MIGRATION_PREPARATION": PortalMigrationPhase.PREPARATION,
    "MIGRATION_READY": PortalMigrationPhase.PARALLEL_OPERATION,
    "MIGRATION_RUNNING": PortalMigrationPhase.FINAL_SYNC,
    "MIGRATION_VERIFICATION": PortalMigrationPhase.RECONCILIATION,
    "LEGACY_REDIRECT": PortalMigrationPhase.OLD_PORTAL_REDIRECT,
}

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
    # Phase 8B — explicit migration window (app TIME_ZONE; never invent machine TZ).
    migration_start_at = models.DateTimeField(null=True, blank=True)
    migration_window_end_at = models.DateTimeField(null=True, blank=True)
    booking_migration_mode = models.CharField(
        max_length=16,
        default="NORMAL",
        help_text=_("NORMAL|PREPARATION|FREEZE|ACTIVE|SETTLEMENT|COMPLETED"),
    )
    new_portal_url = models.URLField(blank=True, default="")
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


class MigrationSettlementType(models.TextChoices):
    MIGRATION_REFUND = "MIGRATION_REFUND", _("Migration refund")


class MigrationSettlementStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    REJECTED = "REJECTED", _("Rejected")


class MigrationBookingSettlement(models.Model):
    """One-time migration financial settlement for a portal booking.

    Money movement MUST go through SubWallet.credit / existing ledger — never
    mutate wallet.balance directly. Successful MIGRATION_REFUND is unique per booking.
    Does not unlock end-user booking freeze or free booking slots.
    """

    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.PROTECT,
        related_name="migration_settlements",
    )
    legacy_booking_id = models.PositiveBigIntegerField(
        db_index=True,
        help_text=_("Portal booking_id at settlement time (audit copy)."),
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        related_name="migration_booking_settlements",
    )
    settlement_type = models.CharField(
        max_length=32,
        choices=MigrationSettlementType.choices,
        default=MigrationSettlementType.MIGRATION_REFUND,
        db_index=True,
    )
    original_amount = models.DecimalField(max_digits=14, decimal_places=2)
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8, default="INR")
    reason = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=MigrationSettlementStatus.choices,
        default=MigrationSettlementStatus.PENDING,
        db_index=True,
    )
    processed_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="migration_settlements_processed",
    )
    processed_by_role = models.CharField(max_length=32, blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    reference = models.CharField(max_length=64, blank=True, default="", db_index=True)
    wallet_transaction = models.ForeignKey(
        "users.SubWalletTransaction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="migration_settlements",
    )
    failure_detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Migration booking settlement")
        verbose_name_plural = _("Migration booking settlements")
        indexes = [
            models.Index(fields=["status", "settlement_type"]),
            models.Index(fields=["legacy_booking_id", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "settlement_type"],
                condition=models.Q(status="COMPLETED"),
                name="uniq_completed_migration_refund_per_booking",
            )
        ]

    def __str__(self) -> str:
        return f"{self.settlement_type} booking={self.legacy_booking_id} {self.status}"


class LegacyEquipmentMappingStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    UNMAPPED = "UNMAPPED", _("Unmapped")
    DISABLED = "DISABLED", _("Disabled")
    CONFLICT = "CONFLICT", _("Conflict")
    RETIRED = "RETIRED", _("Retired")


class LegacyEquipmentMapping(models.Model):
    """Explicit OLD → NEW equipment mapping. No fuzzy runtime name matching."""

    old_equipment_id = models.PositiveBigIntegerField(db_index=True)
    old_equipment_code = models.CharField(max_length=64, blank=True, default="")
    old_equipment_name = models.CharField(max_length=255, blank=True, default="")
    new_equipment = models.ForeignKey(
        "equipment.Equipment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="legacy_equipment_mappings",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_equipment_mappings",
    )
    status = models.CharField(
        max_length=16,
        choices=LegacyEquipmentMappingStatus.choices,
        default=LegacyEquipmentMappingStatus.UNMAPPED,
        db_index=True,
    )
    mapping_reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_equipment_mappings_created",
    )
    updated_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_equipment_mappings_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Legacy equipment mapping")
        verbose_name_plural = _("Legacy equipment mappings")
        constraints = [
            models.UniqueConstraint(
                fields=["old_equipment_id"],
                name="uniq_legacy_equipment_old_id",
            )
        ]

    def __str__(self) -> str:
        return f"old:{self.old_equipment_id}→new:{getattr(self.new_equipment, 'equipment_id', None)} ({self.status})"


class LegacyEquipmentCapacitySplitStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    ACTIVE = "ACTIVE", _("Active")
    DISABLED = "DISABLED", _("Disabled")


class LegacyEquipmentCapacitySplitPolicy(models.TextChoices):
    """Deterministic booking assignment for 1 legacy calendar → N new machines."""

    TIME_BAND_FOLD = "TIME_BAND_FOLD", _("Time-band fold (TG/DTA)")


class LegacyEquipmentCapacitySplit(models.Model):
    """
    Capacity split: one legacy equipment calendar → two (or more) new machines.

    TG/DTA policy (TIME_BAND_FOLD):
      Old overnight slots 00:00/02:15/04:30/06:45 → target_b at 09:00/11:15/13:30/15:45
      Old daytime slots 09:00/11:15/13:30/15:45 → target_a at same wall-clock
    """

    old_equipment_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    old_equipment_code = models.CharField(max_length=64, blank=True, default="")
    old_equipment_name = models.CharField(max_length=255, blank=True, default="")
    target_a = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="legacy_capacity_splits_as_a",
    )
    target_b = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="legacy_capacity_splits_as_b",
    )
    policy = models.CharField(
        max_length=32,
        choices=LegacyEquipmentCapacitySplitPolicy.choices,
        default=LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD,
    )
    status = models.CharField(
        max_length=16,
        choices=LegacyEquipmentCapacitySplitStatus.choices,
        default=LegacyEquipmentCapacitySplitStatus.DRAFT,
        db_index=True,
    )
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_capacity_splits_created",
    )
    updated_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_capacity_splits_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Legacy equipment capacity split")
        verbose_name_plural = _("Legacy equipment capacity splits")

    def __str__(self) -> str:
        return (
            f"split old:{self.old_equipment_id} → "
            f"A={getattr(self.target_a, 'equipment_id', None)} "
            f"B={getattr(self.target_b, 'equipment_id', None)} ({self.status})"
        )


class LegacyBookingMigrationBatchStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    VALIDATED = "VALIDATED", _("Validated")
    ARMED = "ARMED", _("Armed")
    ACTIVE = "ACTIVE", _("Active")
    COMPLETED = "COMPLETED", _("Completed")
    ABORTED = "ABORTED", _("Aborted")


class LegacyBookingMigrationBatch(models.Model):
    """Auditable migration batch for legacy booking slot protection."""

    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=LegacyBookingMigrationBatchStatus.choices,
        default=LegacyBookingMigrationBatchStatus.DRAFT,
        db_index=True,
    )
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="legacy_booking_migration_batches",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Legacy booking migration batch")
        verbose_name_plural = _("Legacy booking migration batches")
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"Batch {self.pk} {self.status}"


class LegacyBookingBlockStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    RELEASED = "RELEASED", _("Released")
    CONFLICT = "CONFLICT", _("Conflict")
    CANCELLED = "CANCELLED", _("Cancelled")


class LegacyUserMappingStatus(models.TextChoices):
    """User identity on a legacy block — independent of slot occupancy."""

    UNRESOLVED = "UNRESOLVED", _("Unresolved")
    RESOLVED_CHANNEL_I = "RESOLVED_CHANNEL_I", _("Resolved via Channel-I")
    NOT_REQUIRED_FOR_BLOCK = "NOT_REQUIRED_FOR_BLOCK", _("Not required for block")


class LegacyBookingBlock(models.Model):
    """Migration reservation metadata. Occupancy is enforced by DailySlot.BLOCKED.

    Do not treat this as a normal Booking. Slot IDs claimed are stored for release/abort.
    """

    BLOCKED_LABEL_PREFIX = "LEGACY_MIGRATION:"

    legacy_booking_id = models.PositiveBigIntegerField(db_index=True)
    legacy_user_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    legacy_employee_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    legacy_equipment_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    new_equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.PROTECT,
        related_name="legacy_booking_blocks",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    source_status = models.CharField(max_length=32, blank=True, default="")
    user_mapping_status = models.CharField(
        max_length=32,
        choices=LegacyUserMappingStatus.choices,
        default=LegacyUserMappingStatus.NOT_REQUIRED_FOR_BLOCK,
        db_index=True,
    )
    user_mapping_source = models.CharField(max_length=64, blank=True, default="")
    resolved_user = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_legacy_booking_blocks",
    )
    source = models.CharField(max_length=32, default="LEGACY_PORTAL")
    status = models.CharField(
        max_length=16,
        choices=LegacyBookingBlockStatus.choices,
        default=LegacyBookingBlockStatus.ACTIVE,
        db_index=True,
    )
    migration_batch = models.ForeignKey(
        LegacyBookingMigrationBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="blocks",
    )
    slot_ids = models.JSONField(default=list, blank=True)
    legacy_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = _("Legacy booking block")
        verbose_name_plural = _("Legacy booking blocks")
        constraints = [
            models.UniqueConstraint(
                fields=["legacy_booking_id", "source"],
                condition=models.Q(status="ACTIVE"),
                name="uniq_active_legacy_booking_block",
            )
        ]
        indexes = [
            models.Index(fields=["new_equipment", "status", "start_at", "end_at"]),
        ]

    def __str__(self) -> str:
        return f"LegacyBlock {self.legacy_booking_id} {self.status}"

    @property
    def blocked_label(self) -> str:
        return f"{self.BLOCKED_LABEL_PREFIX}{self.legacy_booking_id}"


class MigrationNotificationTemplate(models.TextChoices):
    FACULTY_MIGRATION = "FACULTY_MIGRATION", _("Faculty migration")
    STUDENT_MIGRATION = "STUDENT_MIGRATION", _("Student migration")
    OIC_MIGRATION = "OIC_MIGRATION", _("OIC migration")
    ADMIN_MIGRATION = "ADMIN_MIGRATION", _("Main Administrator migration")


class MigrationNotificationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    QUEUED = "QUEUED", _("Queued")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Skipped")


class MigrationNotificationBatch(models.Model):
    """Auditable email batch for Phase 8C migration communication (async via Celery)."""

    migration_batch = models.ForeignKey(
        LegacyBookingMigrationBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notification_batches",
    )
    dry_run = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default="DRAFT", db_index=True)
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="migration_notification_batches_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Migration notification batch")
        verbose_name_plural = _("Migration notification batches")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"NotificationBatch {self.pk} {self.status}"


class MigrationNotificationRecipient(models.Model):
    """One recipient row per migration notification batch (idempotent per batch+user)."""

    batch = models.ForeignKey(
        MigrationNotificationBatch,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    user = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="migration_notifications",
    )
    recipient_email = models.EmailField()
    role = models.CharField(max_length=32, blank=True, default="")
    template = models.CharField(
        max_length=32,
        choices=MigrationNotificationTemplate.choices,
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=MigrationNotificationStatus.choices,
        default=MigrationNotificationStatus.PENDING,
        db_index=True,
    )
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    failure_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Migration notification recipient")
        verbose_name_plural = _("Migration notification recipients")
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "user"],
                name="uniq_migration_notification_batch_user",
            ),
            models.UniqueConstraint(
                fields=["batch", "recipient_email", "template"],
                name="uniq_migration_notification_batch_email_template",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "template"]),
        ]

    def __str__(self) -> str:
        return f"{self.template} → {self.recipient_email} ({self.status})"


class MigrationT0Event(models.Model):
    """Immutable audit row for staging/production T0 simulation steps."""

    environment = models.CharField(max_length=32, default="STAGING")
    t0_at = models.DateTimeField(null=True, blank=True)
    booking_migration_mode = models.CharField(max_length=16, blank=True, default="")
    migration_batch = models.ForeignKey(
        LegacyBookingMigrationBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="t0_events",
    )
    notification_batch = models.ForeignKey(
        MigrationNotificationBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="t0_events",
    )
    steps = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="migration_t0_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = _("Migration T0 event")
        verbose_name_plural = _("Migration T0 events")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"T0 {self.environment} {self.t0_at}"
