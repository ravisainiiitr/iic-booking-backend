"""Create the website policy pages as unpublished drafts (Terms, Privacy, Refunds, About). Existing pages with these
slugs are never changed, so an approved and edited page is safe from a re-run. ``--dry-run`` only reports."""

from django.core.management.base import BaseCommand

from iic_booking.cms.models import CmsPage
from iic_booking.cms.policy_pages import POLICY_PAGES, draft_content


class Command(BaseCommand):
    help = "Create the website policy pages (Terms, Privacy, Refunds, About) as unpublished drafts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        for slug, title in POLICY_PAGES:
            existing = CmsPage.objects.filter(slug=slug).first()
            if existing:
                self.stdout.write(f"{slug}: exists ({'published' if existing.is_published else 'draft'}), unchanged")
                continue
            if options["dry_run"]:
                self.stdout.write(f"{slug}: would be created as a draft")
                continue
            CmsPage.objects.create(slug=slug, title=title, content=draft_content(slug), is_published=False)
            self.stdout.write(f"{slug}: created as a draft (unpublished)")
