"""Compare the template catalogue with Meta, and submit what is missing. Never sends a WhatsApp message.

  manage.py whatsapp_sync_templates --dry-run                     # the default: changes nothing, here or at Meta
  manage.py whatsapp_sync_templates --dry-run --show-wording
  manage.py whatsapp_sync_templates --status                      # store Meta's statuses, then report
  manage.py whatsapp_sync_templates --submit --batch-size 5       # submit missing templates, one batch
  manage.py whatsapp_sync_templates --correct iitr_payment_failed # one controlled edit of a rejected template

Output goes to public run logs, so it prints template names, statuses and wording only: no IDs or tokens.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from iic_booking.whatsapp import templates
from iic_booking.whatsapp.template_catalogue import CATALOGUE
from iic_booking.whatsapp.template_client import TemplateApiError, TemplateClient, clean


class Command(BaseCommand):
    help = "Compare WhatsApp templates with Meta and submit the missing ones. Dry run unless --submit is given."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Show what would be submitted. The default.")
        mode.add_argument("--submit", action="store_true", help="Submit missing templates, one batch.")
        mode.add_argument("--status", action="store_true", help="Store Meta's statuses and print the report.")
        mode.add_argument("--correct", default="", help="Edit one rejected template, after its wording changed in the catalogue.")
        parser.add_argument("--batch-size", type=int, default=templates.DEFAULT_BATCH, help=f"At most {templates.MAX_BATCH}.")
        parser.add_argument("--delay", type=float, default=templates.DEFAULT_DELAY_SECONDS, help="Seconds between submissions.")
        parser.add_argument("--show-wording", action="store_true", help="With --dry-run: print each template's text.")

    def handle(self, *args, **options):
        client = TemplateClient()
        actor = "manage.py whatsapp_sync_templates"
        try:
            templates.check_ready(client)
            if options["submit"]:
                self._submit(client, options, actor)
            elif options["status"]:
                self._status(client, actor)
            elif options["correct"]:
                result = templates.correct_rejected(client, options["correct"], actor=actor)
                self.stdout.write(f"CORRECTED {result['name']}: sent for review, attempt {result['attempt']}")
            else:
                self._dry_run(client, options)
        except (templates.TemplateConfigError, TemplateApiError) as exc:
            kind = getattr(exc, "kind", "")
            raise CommandError(f"{kind + ': ' if kind else ''}{clean(exc)}") from exc

    def _dry_run(self, client, options):
        plan = templates.build_plan(client)
        self.stdout.write(f"DRY RUN: nothing changed. {len(plan.meta_rows)} at Meta, {len(CATALOGUE)} in the catalogue.")
        self.stdout.write(f"Would submit: {len(plan.to_submit)}")
        for template in plan.to_submit:
            self.stdout.write(f"  SUBMIT {template.name} [{template.language}, {template.category}] event={template.event}")
            if options["show_wording"]:
                if template.is_authentication:  # Meta writes this wording itself: the payload carries only options
                    self.stdout.write(f"    BODY (Meta's wording): {template.body}")
                    self.stdout.write(f"    FOOTER (Meta's wording): {template.footer}")
                    self.stdout.write("    BUTTON: Copy code")
                else:
                    for part in template.payload()["components"]:
                        self.stdout.write(f"    {part['type']}: {part['text']}")
                self.stdout.write(f"    EXAMPLES: {list(template.examples)}")
        self.stdout.write(f"Already at Meta, would skip: {len(plan.existing)}")
        for template, row in plan.existing:
            note = " WORDING DIFFERS FROM THE CATALOGUE" if templates.wording_differs(template, row) else ""
            self.stdout.write(f"  SKIP {template.name} [{clean(row.get('status'))}]{note}")
        if plan.not_in_catalogue:
            self.stdout.write(f"At Meta but not in the catalogue, left alone: {len(plan.not_in_catalogue)}")
            for row in plan.not_in_catalogue:
                self.stdout.write(f"  OTHER {clean(row.get('name'))} [{clean(row.get('status'))}]")

    def _submit(self, client, options, actor):
        result = templates.submit_missing(client, batch_size=options["batch_size"], delay=options["delay"], actor=actor)
        self.stdout.write(f"Submitted: {len(result['submitted'])}")
        for name in result["submitted"]:
            self.stdout.write(f"  SUBMITTED {name}")
        self.stdout.write(f"Skipped, already at Meta: {len(result['skipped_existing'])}")
        for item in result["failed"]:
            self.stdout.write(f"  FAILED {item['name']}: {item['kind']}: {item['message']}")
        if result["stopped"]:
            self.stdout.write(f"STOPPED early: {result['stopped']}")
        self.stdout.write(f"Still to submit: {result['remaining']}")
        if result["failed"]:
            raise CommandError(f"{len(result['failed'])} template(s) failed; see above")

    def _status(self, client, actor):
        plan = templates.sync_statuses(client, actor=actor)
        print_report(self.stdout, templates.build_report(meta_rows=plan.meta_rows))


def print_report(out, report: dict) -> None:
    out.write(f"Last synchronised: {report['last_synced_at'] or 'never'}")
    for title, key in (("Approved", "approved"), ("Pending", "pending"), ("Rejected", "rejected"),
                       ("Paused or disabled", "paused_or_disabled"), ("Missing", "missing")):
        out.write(f"{title}: {len(report[key])}")
        for item in report[key]:
            reason = f" reason={item['rejected_reason']}" if item.get("rejected_reason") else ""
            suggestion = f" suggestion: {item['suggestion']}" if item.get("suggestion") else ""
            out.write(f"  {item['name']} [{item['language']}, {item['category']}] {item['status']}{reason}{suggestion}")
    out.write(f"Requires correction: {len(report['requires_correction'])}")
    for item in report["requires_correction"]:
        out.write(f"  {item['name']}: {item['why']}. {item.get('suggestion', '')}".rstrip())
    out.write(f"In the catalogue, but nothing in the portal sends them yet: {len(report['not_wired'])}")
    for item in report["not_wired"]:
        out.write(f"  {item['name']}")
