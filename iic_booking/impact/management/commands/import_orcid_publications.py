"""Save a researcher's ORCID iD and suggest the papers whose author affiliations match a phrase.

Dry run by default: lists what would be suggested and writes nothing. ``--apply`` saves the ORCID iD on the account and
stores the matching papers as NEW suggestions in the researcher's "Find my papers" list (nothing is claimed: the
researcher picks the equipment and claims each paper as usual).

    python manage.py import_orcid_publications --user-id 42 --orcid 0000-0002-1825-0097 \\
        --affiliation "Instrumentation Cent(re|er)" [--since-year 2010] [--apply]
"""

import re
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from iic_booking.impact import service
from iic_booking.impact.models import ResearcherOrcid


class Command(BaseCommand):
    help = "Suggest a researcher's ORCID-listed papers whose author affiliations match a phrase (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--orcid", required=True)
        parser.add_argument("--affiliation", required=True,
                            help="Case-insensitive regular expression matched against author affiliations.")
        parser.add_argument("--since-year", type=int, default=1900, help="Only papers published in or after this year.")
        parser.add_argument("--apply", action="store_true", help="Save the ORCID iD and the suggestions.")

    def handle(self, *args, **opts):
        user = get_user_model().objects.filter(pk=opts["user_id"]).first()
        if user is None:
            raise CommandError(f"No user with id {opts['user_id']}.")
        try:
            orcid = service.normalise_orcid(opts["orcid"])
        except service.ImpactError as exc:
            raise CommandError(exc.message) from None
        if ResearcherOrcid.objects.filter(orcid=orcid).exclude(user=user).exists():
            raise CommandError("This ORCID iD is already linked to another account.")
        try:
            pattern = re.compile(opts["affiliation"], re.I)
        except re.error as exc:
            raise CommandError(f"--affiliation is not a valid pattern: {exc}") from None

        works = service._fetch_openalex(orcid, date(opts["since_year"], 1, 1))
        matched = []
        for w in works:
            aff = service.affiliation_matches(w, pattern)
            if aff:
                matched.append((w, aff))
        matched.sort(key=lambda m: -(m[0].get("publication_year") or 0))

        mode = "APPLY" if opts["apply"] else "DRY RUN"
        self.stdout.write(f"{mode} user={user.pk} type={getattr(user, 'user_type', '-')} orcid={orcid}")
        self.stdout.write(f"works found={len(works)} affiliation matches={len(matched)}")
        for w, aff in matched:
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            self.stdout.write(f"  {w.get('publication_year')} | {(w.get('title') or '')[:100]} | {doi or '-'}")
            self.stdout.write(f"      affiliation: {aff[:160]}")

        if not opts["apply"]:
            self.stdout.write("Dry run: nothing saved. Re-run with --apply to save.")
            return
        with transaction.atomic():
            service.set_orcid(user, orcid)
            added = service.store_suggestions(user, [w for w, _ in matched])
        self.stdout.write(self.style.SUCCESS(
            f"Saved ORCID iD and {added} new suggestion(s); {len(matched) - added} already claimed or suggested."))
