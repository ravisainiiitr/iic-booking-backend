"""Report WhatsApp template statuses from what is stored; --refresh asks Meta first (GET only). Never sends a message."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from iic_booking.whatsapp import templates
from iic_booking.whatsapp.template_client import TemplateApiError, TemplateClient, clean

from .whatsapp_sync_templates import print_report


class Command(BaseCommand):
    help = "Approved, pending, rejected and missing WhatsApp templates, and which need correcting."

    def add_arguments(self, parser):
        parser.add_argument("--refresh", action="store_true", help="Fetch the latest statuses from Meta first.")

    def handle(self, *args, **options):
        rows = None
        if options["refresh"]:
            client = TemplateClient()
            try:
                templates.check_ready(client)
                rows = templates.sync_statuses(client, actor="manage.py whatsapp_template_report").meta_rows
            except (templates.TemplateConfigError, TemplateApiError) as exc:
                raise CommandError(clean(exc)) from exc
        print_report(self.stdout, templates.build_report(meta_rows=rows))
