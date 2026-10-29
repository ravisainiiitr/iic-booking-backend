"""Run I-STEM work on demand: queue eligible bookings and/or process due outbox jobs.

  python manage.py istem_sync --enqueue
  python manage.py istem_sync --process [--limit 20]

Nothing schedules this command. In shadow mode payloads are validated and recorded but nothing is sent.
"""

from django.core.management.base import BaseCommand, CommandError

from iic_booking.istem import conf
from iic_booking.istem.service import enqueue_eligible, enqueue_eligible_executions, process_due_jobs


class Command(BaseCommand):
    help = "Queue eligible bookings for I-STEM and/or process due I-STEM outbox jobs."

    def add_arguments(self, parser):
        parser.add_argument("--enqueue", action="store_true",
                            help="Queue Create Booking for eligible bookings, and FBR Execute for completed ones (if enabled).")
        parser.add_argument("--process", action="store_true", help="Process due outbox jobs.")
        parser.add_argument("--limit", type=int, default=20, help="Maximum bookings to queue / jobs to process.")

    def handle(self, *args, **options):
        mode = conf.istem_mode()
        if mode == conf.MODE_OFF:
            raise CommandError("The I-STEM integration is off (ISTEM_ENABLED / ISTEM_MODE); nothing to do.")
        if not options["enqueue"] and not options["process"]:
            raise CommandError("Pass --enqueue and/or --process.")
        if mode == conf.MODE_LIVE and conf.missing_live_settings():
            raise CommandError("Live mode needs: " + ", ".join(conf.missing_live_settings()))
        self.stdout.write(f"I-STEM mode: {mode}")
        if options["enqueue"]:
            self.stdout.write(f"Queued: {enqueue_eligible(limit=options['limit'])}")
            self.stdout.write(f"Queued FBR Execute: {enqueue_eligible_executions(limit=options['limit'])}")
        if options["process"]:
            results = process_due_jobs(limit=options["limit"])
            summary = {}
            for state in results.values():
                summary[state] = summary.get(state, 0) + 1
            self.stdout.write(f"Processed {len(results)} job(s): {summary or 'none due'}")
