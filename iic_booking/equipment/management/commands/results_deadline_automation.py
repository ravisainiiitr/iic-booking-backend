from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment.results_deadline import automation_state, dry_run, dry_run_report_text, set_automation


class Command(BaseCommand):
    help = (
        "Results-deadline safeguard: 'status' / 'dry-run' (read-only), 'enable' (refused unless the dry run "
        "shows no existing booking would be acted on), 'disable'."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "dry-run", "enable", "disable"])

    def handle(self, *args, **options):
        action = options["action"]
        if action == "status":
            state = automation_state()
            self.stdout.write(f"automation_enabled={state.enabled} since={state.since.isoformat() if state.since else None}")
            return
        if action == "dry-run":
            self.stdout.write(dry_run_report_text())
            return
        if action == "disable":
            set_automation(False)
            self.stdout.write("Results-deadline automation disabled; the old per-equipment timers apply to all bookings.")
            return

        data = dry_run()
        affected = sum(data["would_act_if_enabled_now"].values()) + sum(
            data["would_act_if_applied_to_all_existing"].values()
        )
        self.stdout.write(dry_run_report_text())
        if affected:
            raise CommandError(
                f"Not enabled: the dry run shows {affected} existing booking action(s). Review them first."
            )
        row = set_automation(True)
        self.stdout.write(
            f"Results-deadline automation enabled for bookings whose slot ends from {row.automation_since.isoformat()}."
        )
