"""Sync CommunicationTemplate email rows from default_email_templates catalog."""

from django.core.management.base import BaseCommand

from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.models import CommunicationTemplate


class Command(BaseCommand):
    help = (
        "Create or update default email CommunicationTemplate rows from the "
        "branded catalog (email_branding / default_email_templates)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the database.",
        )
        parser.add_argument(
            "--codes",
            nargs="*",
            default=None,
            help="Optional template codes to sync (default: all catalog emails).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only_codes = set(options["codes"] or [])
        created = updated = skipped = 0

        for spec in get_default_email_templates():
            code = spec["code"]
            if only_codes and code not in only_codes:
                continue
            existing = CommunicationTemplate.objects.filter(
                code=code,
                communication_type=CommunicationTemplate.CommunicationType.EMAIL,
            ).first()
            fields = {
                "name": spec.get("name") or code,
                "subject": spec["subject"],
                "body_text": spec["body_text"],
                "body_html": spec["body_html"],
                "description": spec.get("description") or "",
                "variable_help": spec.get("variable_help") or "",
                "is_active": spec.get("is_active", True),
            }
            if existing is None:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"CREATE {code}"))
                if not dry_run:
                    CommunicationTemplate.objects.create(
                        code=code,
                        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
                        **fields,
                    )
                continue

            changed = any(getattr(existing, k) != v for k, v in fields.items())
            if not changed:
                skipped += 1
                self.stdout.write(f"SKIP   {code} (unchanged)")
                continue
            updated += 1
            self.stdout.write(self.style.WARNING(f"UPDATE {code}"))
            if not dry_run:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.save(update_fields=list(fields.keys()) + ["updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. created={created} updated={updated} skipped={skipped}"
                + (" (dry-run)" if dry_run else "")
            )
        )
