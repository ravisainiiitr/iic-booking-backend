"""Management command to read email via IMAP (test/utility)."""

from django.core.management.base import BaseCommand

from iic_booking.communication.imap_service import get_imap_reader


class Command(BaseCommand):
    help = "Read emails from the configured IMAP mailbox (imap.iitr.ac.in, SSL, normal password)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--folder",
            default="INBOX",
            help="Mailbox folder (default: INBOX)",
        )
        parser.add_argument(
            "--since",
            default=None,
            help='IMAP SINCE date, e.g. "01-Jan-2025"',
        )
        parser.add_argument(
            "--max",
            type=int,
            default=10,
            help="Max number of emails to fetch (default: 10)",
        )
        parser.add_argument(
            "--list-folders",
            action="store_true",
            help="Only list folders and exit",
        )
        parser.add_argument(
            "--mark-seen",
            action="store_true",
            help="Mark fetched messages as read (SEEN)",
        )

    def handle(self, *args, **options):
        try:
            reader = get_imap_reader(mailbox=options["folder"])
            with reader:
                if options["list_folders"]:
                    folders = reader.list_folders()
                    self.stdout.write(self.style.SUCCESS("Folders:"))
                    for f in folders:
                        self.stdout.write(f"  {f}")
                    return

                count = reader.select(mailbox=options["folder"])
                self.stdout.write(self.style.SUCCESS(f"Mailbox has {count} message(s)."))

                emails, mailbox_total = reader.fetch_emails(
                    mailbox=options["folder"],
                    since=options["since"],
                    max_count=options["max"],
                    mark_seen=options["mark_seen"],
                )
                for i, em in enumerate(emails, 1):
                    self.stdout.write("")
                    self.stdout.write(self.style.HTTP_INFO(f"--- Email {i} (uid={em.get('uid')}) ---"))
                    self.stdout.write(f"From: {em.get('from', '')}")
                    self.stdout.write(f"Subject: {em.get('subject', '')}")
                    self.stdout.write(f"Date: {em.get('date_raw', '')}")
                    body = (em.get("body_plain") or em.get("body_html") or "").strip()
                    if body:
                        preview = body[:200] + "..." if len(body) > 200 else body
                        self.stdout.write(f"Body: {preview}")
                self.stdout.write("")
                self.stdout.write(self.style.SUCCESS(f"Fetched {len(emails)} email(s)."))
        except ValueError as e:
            self.stderr.write(self.style.ERROR(f"Configuration error: {e}"))
            raise SystemExit(1)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"IMAP error: {e}"))
            raise SystemExit(1)
