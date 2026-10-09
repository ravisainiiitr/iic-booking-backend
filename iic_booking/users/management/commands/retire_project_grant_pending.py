from django.core.management.base import BaseCommand, CommandError

from iic_booking.users import project_grant_retirement


class Command(BaseCommand):
    help = "Report (and with --apply --confirm RETIRE soft-delete) every PENDING Project Grant recharge request."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm", default="")

    def handle(self, *args, **options):
        try:
            report = project_grant_retirement.run(apply=options["apply"], confirm=options["confirm"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for key, value in report.items():
            self.stdout.write(f"{key}={value}")
