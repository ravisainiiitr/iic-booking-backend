"""Read the open-access acknowledgement of researchers' suggested papers and map them to portal facilities.

Dry run unless --apply. With --apply: evidence and mappings are stored, and equipment-level evidence above the
threshold becomes a pending claim for the OIC (never approved here). Suggestions are not changed.

    python manage.py verify_publication_acknowledgements --user-id 1911 [--limit 50] [--apply] [--notify]
    python manage.py verify_publication_acknowledgements --doi 10.1000/x --doi 10.1000/y   # papers only, no user
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from iic_booking.equipment.publication_dedupe import normalize_doi
from iic_booking.impact import facility_mapping as fm


class Command(BaseCommand):
    help = "Check open-access acknowledgements and map papers to departments and equipment (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, action="append", default=[])
        parser.add_argument("--doi", action="append", default=[], help="Only these DOIs (with --user-id), or check them alone.")
        parser.add_argument("--doi-file", help="File with one DOI per line (papers-only check).")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--recheck", action="store_true", help="Also papers that already have evidence.")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--notify", action="store_true", help="With --apply: tell the OICs about new claims.")
        parser.add_argument("--quiet", action="store_true", help="Only the summary.")

    def _print(self, res):
        if self.quiet:
            return
        self.stdout.write(f"  [{res.status}] {res.year or '-'} | {res.title[:90]} | {res.doi or '-'}")
        if res.source_url:
            self.stdout.write(f"      source: {res.source_url[:150]} ({res.oa_status or '-'}; licence {res.licence or '-'})")
        if res.detail and res.status != fm.EvidenceStatus.MATCHED:
            self.stdout.write(f"      {res.detail[:200]}")
        for m in res.matches:
            target = f"equipment {m.equipment_id}" if m.equipment_id else f"department {m.department_id}"
            self.stdout.write(f"      -> {m.level:<10} {target:<15} {m.confidence:.3f} {m.rationale[:160]}")
        if res.snippet and res.matches:
            self.stdout.write(f"      excerpt: “{res.snippet[:300]}”")

    def _summary(self, label, s):
        self.stdout.write(f"{label}: papers={s['papers']} {s['by_status']} department mappings={s['department_mappings']} "
                          f"equipment mappings={s['equipment_mappings']} claims to create={s['claims_to_create']} "
                          f"claims created={s['claims_created']}")

    def handle(self, *args, **opts):
        self.quiet = opts["quiet"]
        mode = "APPLY" if opts["apply"] else "DRY RUN"
        dois = [normalize_doi(d) for d in opts["doi"]]
        if opts["doi_file"]:
            dois += [normalize_doi(line) for line in open(opts["doi_file"], encoding="utf-8") if line.strip()]
        dois = [d for d in dict.fromkeys(dois) if d]
        if not fm.load_aliases():
            self.stdout.write("No active facility aliases: run seed_facility_aliases --apply first (or add aliases).")

        if not opts["user_id"]:
            if not dois:
                raise CommandError("Give --user-id, or --doi / --doi-file for a papers-only check.")
            if opts["apply"]:
                raise CommandError("A papers-only check cannot --apply: there is no researcher to attach it to.")
            works = fm.openalex_works_by_doi(dois)
            aliases = fm.load_aliases()
            s = {"papers": len(dois), "by_status": {}, "department_mappings": 0, "equipment_mappings": 0,
                 "claims_to_create": 0, "claims_created": 0}
            self.stdout.write(f"{mode} papers-only: {len(dois)} DOI(s)")
            for d in dois:
                w = works.get(d)
                res = fm.check_paper(d, (w or {}).get("title") or d, (w or {}).get("publication_year"), w, aliases)
                s["by_status"][res.status] = s["by_status"].get(res.status, 0) + 1
                s["department_mappings"] += sum(1 for m in res.matches if m.level == "DEPARTMENT")
                s["equipment_mappings"] += sum(1 for m in res.matches if m.level == "EQUIPMENT")
                s["claims_to_create"] += any(m.level == "EQUIPMENT" and m.confidence >= fm.claim_threshold() for m in res.matches)
                self._print(res)
            self._summary("papers-only", s)
            return

        for u in get_user_model().objects.filter(pk__in=opts["user_id"]).order_by("pk"):
            self.stdout.write(f"{mode} user={u.pk} type={u.user_type}")
            s = fm.verify_user(u, apply=opts["apply"], dois=dois or None, limit=opts["limit"], recheck=opts["recheck"],
                               notify=opts["notify"], log=self._print)
            self._summary(f"user={u.pk}", s)
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing saved. Re-run with --apply to save.")
