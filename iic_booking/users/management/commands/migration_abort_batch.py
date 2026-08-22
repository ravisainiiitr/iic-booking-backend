"""Abort a LegacyBookingMigrationBatch (releases ACTIVE blocks; keeps audit)."""

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.booking_bridge import abort_migration_batch
from iic_booking.users.models.portal_migration import (
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
)


class Command(BaseCommand):
    help = "Abort a migration batch: release ACTIVE blocks; preserve audit. No financial reversal."

    def add_arguments(self, parser):
        parser.add_argument("batch_id", type=int)
        parser.add_argument("--confirm-abort", action="store_true")
        parser.add_argument("--reason", type=str, default="aborted")

    def handle(self, *args, **options):
        if not options.get("confirm_abort"):
            raise CommandError("Pass --confirm-abort to abort a batch.")
        try:
            batch = LegacyBookingMigrationBatch.objects.get(pk=options["batch_id"])
        except LegacyBookingMigrationBatch.DoesNotExist as exc:
            raise CommandError("Batch not found") from exc
        if batch.status == LegacyBookingMigrationBatchStatus.COMPLETED:
            raise CommandError(
                "Batch COMPLETED — abort will not reverse Phase-8A refunds. "
                "Use approved financial workflow for any money reversal."
            )
        result = abort_migration_batch(batch, reason=options.get("reason") or "aborted")
        self.stdout.write(str(result))
