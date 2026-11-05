"""Generate facility aliases from department and equipment names, codes and categories, the acknowledgement policy and
funding acknowledgement phrases. Dry run unless --apply; existing aliases (including switched-off ones) are kept.

Also switches off generated equipment aliases that name a department (e.g. "IIC" taken from "Electronic Repair lab
(IIC)") and re-matches papers waiting for review against the corrected aliases."""

from django.core.management.base import BaseCommand

from iic_booking.impact.facility_mapping import rescore_pending, seed_aliases, switch_off_clashes


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
        clashes = switch_off_clashes(apply=opts["apply"])
        self.stdout.write(f"{mode} equipment aliases naming a department, switched off: {len(clashes)}")
        for a in clashes:
            self.stdout.write(f"  alias {a.pk}: \"{a.phrase}\" on equipment {a.equipment_id} {a.equipment.name}")
        changes = rescore_pending(apply=opts["apply"], skip_alias_ids={a.pk for a in clashes})
        self.stdout.write(f"{mode} papers waiting for review re-matched with changes: {len(changes)}")
        for ch in changes:
            self.stdout.write(f"  evidence {ch['evidence_id']}: removed={ch['removed']} added={ch['added']}"
                              + (f" still waiting={ch['still_pending']}" if "still_pending" in ch else ""))
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing saved. Re-run with --apply to save.")
