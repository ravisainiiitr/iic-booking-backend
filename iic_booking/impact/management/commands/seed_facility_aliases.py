"""Generate facility aliases from department and equipment names, codes and categories, the acknowledgement policy and
funding acknowledgement phrases. Dry run unless --apply; existing aliases (including switched-off ones) are kept."""

from django.core.management.base import BaseCommand

from iic_booking.impact.facility_mapping import seed_aliases


class Command(BaseCommand):
    help = "Generate facility aliases for publication acknowledgement matching (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--show", type=int, default=200, help="How many generated aliases to list.")

    def handle(self, *args, **opts):
        out = seed_aliases(apply=opts["apply"])
        mode = "APPLY" if opts["apply"] else "DRY RUN"
        self.stdout.write(f"{mode} generated={out['generated']} new={out['new']} already present={out['existing']}")
        for r in out["rows"][: opts["show"]]:
            target = f"department {r['department_id']}" if r["department_id"] else f"equipment {r['equipment_id']}"
            self.stdout.write(f"  {r['source']:<8} {target:<16} {'Aa' if r['case_sensitive'] else '  '} {r['phrase']}")
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing saved. Re-run with --apply to save.")
