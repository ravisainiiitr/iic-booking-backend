"""
Safe test-account cleanup wrapper for Phase 8B.

Uses ONLY User.is_test_account=True. Never classifies by email/name patterns.

Dry-run (default):
  python manage.py migration_cleanup_test_accounts --dry-run

Actual cleanup (delegates to clear_test_account_data):
  python manage.py migration_cleanup_test_accounts --confirm-test-cleanup
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.models import User


class Command(BaseCommand):
    help = (
        "Safe test-account cleanup using explicit is_test_account flag only. "
        "Default is dry-run. Requires --confirm-test-cleanup to delete."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Preview only (also the default when confirm flag is absent).",
        )
        parser.add_argument(
            "--confirm-test-cleanup",
            action="store_true",
            help="Perform cleanup via clear_test_account_data.",
        )
        parser.add_argument(
            "--delete-users",
            action="store_true",
            help="Also delete test user rows after wiping activity.",
        )

    def handle(self, *args, **options):
        test_count = User.objects.filter(is_test_account=True).count()
        real_count = User.objects.filter(is_test_account=False).count()
        self.stdout.write(f"test_users_found={test_count}")
        self.stdout.write(f"real_users_untouched={real_count}")
        if test_count == 0:
            self.stdout.write(self.style.WARNING("No is_test_account users found. STOP — nothing to clean."))
            return
        # Ambiguity guard: never infer test users without the flag.
        if not options.get("confirm_test_cleanup"):
            self.stdout.write(self.style.WARNING("DRY-RUN only. Pass --confirm-test-cleanup to delete."))
            call_command("clear_test_account_data", stdout=self.stdout, stderr=self.stderr)
            return
        # Delegate with exact confirm token used by existing safe command.
        kwargs = {"confirm": "CLEAR_TEST_ACCOUNT_DATA"}
        if options.get("delete_users"):
            kwargs["delete_users"] = True
        call_command("clear_test_account_data", stdout=self.stdout, stderr=self.stderr, **kwargs)
        after = User.objects.filter(is_test_account=True).count()
        self.stdout.write(f"test_users_after={after}")
        if User.objects.filter(is_test_account=False).count() != real_count:
            raise CommandError("SAFETY FAILURE: real user count changed — investigate immediately.")
        self.stdout.write(self.style.SUCCESS("Test cleanup completed; real users unchanged."))
