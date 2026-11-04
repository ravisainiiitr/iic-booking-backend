"""Find OpenAlex author records for faculty by name and the institute's ROR ID. Dry run unless --apply.

    python manage.py link_faculty_authors --user-id 12 --user-id 40 [--ror https://ror.org/...] [--apply] [--discover]
    python manage.py link_faculty_authors --department CHEM --limit 20 [--without-orcid]
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from iic_booking.impact import author_linking
from iic_booking.impact.models import ResearcherOrcid


class Command(BaseCommand):
    help = "Match faculty to OpenAlex author records (unambiguous ones linked, others left for confirmation)."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, action="append", default=[])
        parser.add_argument("--department", help="Department code: its active faculty.")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--without-orcid", action="store_true", help="Skip faculty who saved an ORCID iD.")
        parser.add_argument("--ror", default="", help="Overrides INSTITUTE_ROR_ID.")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--discover", action="store_true", help="With --apply: suggest the linked authors' works.")

    def handle(self, *args, **opts):
        from iic_booking.users.models.user_type import UserType

        User = get_user_model()
        if opts["user_id"]:
            users = list(User.objects.filter(pk__in=opts["user_id"]))
        elif opts["department"]:
            qs = User.objects.filter(user_type=UserType.FACULTY, is_active=True, department__code=opts["department"])
            if opts["without_orcid"]:
                qs = qs.exclude(pk__in=ResearcherOrcid.objects.values("user_id"))
            users = list(qs.order_by("pk")[: opts["limit"]])
        else:
            raise CommandError("Give --user-id or --department.")
        mode = "APPLY" if opts["apply"] else "DRY RUN"
        totals = {"users": 0, "auto": 0, "confirm": 0, "none": 0}
        for u in users:
            try:
                cands = author_linking.link_user(u, apply=opts["apply"], ror=opts["ror"] or None)
            except author_linking.AuthorLinkError as exc:
                raise CommandError(str(exc)) from None
            except Exception as exc:  # network
                self.stdout.write(f"{mode} user={u.pk}: lookup failed ({type(exc).__name__})")
                continue
            totals["users"] += 1
            auto = [c for c in cands if c["status"] == "AUTO_LINKED"]
            totals["auto"] += bool(auto)
            totals["confirm"] += bool(cands) and not auto
            totals["none"] += not cands
            outcome = "linked" if auto else ("needs confirmation" if cands else "no candidate")
            self.stdout.write(f"{mode} user={u.pk} type={u.user_type} dept={getattr(u.department, 'code', '-')}: {outcome}")
            for c in cands[:5]:
                self.stdout.write(f"    {c['status']:<18} {c['confidence']:.3f} {c['openalex_author_id']} {c['display_name']} "
                                  f"works={c['works_count']} orcid={c['orcid'] or '-'} | {c['rationale']}")
            if opts["apply"] and opts["discover"] and auto:
                self.stdout.write(f"    discovery: {author_linking.discover_linked(u)}")
        self.stdout.write(f"users={totals['users']} linked={totals['auto']} needs confirmation={totals['confirm']} "
                          f"no candidate={totals['none']}")
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing saved. Re-run with --apply to save.")
