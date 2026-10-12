import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users import account_lifecycle as lc


class Command(BaseCommand):
    help = (
        "Programme / employment end dates. 'report' and 'degree-names' are read-only counts (no names or "
        "addresses). 'recompute' and 'backfill-links' are dry runs unless --apply is given. 'run-daily' runs the "
        "daily task now (reminders and suspension notices only when ACCOUNT_END_DATE_ENFORCEMENT is on)."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["report", "degree-names", "recompute", "backfill-links", "run-daily"])
        parser.add_argument("--apply", action="store_true", help="Write changes (recompute / backfill-links).")

    def handle(self, *args, **options):
        action = options["action"]
        apply = bool(options["apply"])
        if not lc.schema_ready():
            raise CommandError("Account lifecycle tables are not migrated yet (users 0139).")
        if action == "report":
            data = lc.report()
            data.pop("degree_names_top", None)
            self._dump(data)
        elif action == "degree-names":
            data = lc.report(top_degrees=500)
            self._dump({"degree_names_distinct": data["degree_names_distinct"], "degree_names": data["degree_names_top"]})
        elif action == "recompute":
            self._dump(lc.recompute_all(apply=apply))
        elif action == "backfill-links":
            self._dump(lc.backfill_links(apply=apply))
        else:
            self._dump(lc.run_daily())

    def _dump(self, data):
        self.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str))
