"""
Send a one-off announcement email to every active IIT Roorkee faculty member, one email each.

Dry run by default (counts, exclusions, masked sample). Modes:
  --test-to ADDRESS               one [TEST] copy to ADDRESS (CC / BCC only with --test-include-cc)
  --send --confirm <campaign>     send to all faculty not yet sent (re-runs skip those already sent)
  --write-preview DIR             write preview.html / preview.txt for review

CC / BCC / Reply-To take addresses or the tokens dean_sric, ar_sric, sric_office, bill_section (Wallet SRIC settings).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from iic_booking.communication.announcements import CAMPAIGNS
from iic_booking.communication.announcements import AnnouncementError
from iic_booking.communication.announcements import SendOptions
from iic_booking.communication.announcements import dry_run_report
from iic_booking.communication.announcements import mask_email
from iic_booking.communication.announcements import parse_addresses
from iic_booking.communication.announcements import send_campaign
from iic_booking.communication.announcements import send_test
from iic_booking.communication.announcements import write_preview


class Command(BaseCommand):
    help = "Announcement email to all active IITR faculty (dry run unless --test-to or --send --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("campaign", choices=sorted(CAMPAIGNS))
        parser.add_argument(
            "--send", action="store_true", help="Send to all faculty not yet sent."
        )
        parser.add_argument(
            "--confirm", default="", help="Must equal the campaign name for --send."
        )
        parser.add_argument(
            "--test-to", default="", help="Send one [TEST] copy to this address only."
        )
        parser.add_argument(
            "--test-name", default="Colleague", help="Greeting name in the test copy."
        )
        parser.add_argument(
            "--test-include-cc",
            action="store_true",
            help="Also copy CC / BCC on the test email.",
        )
        parser.add_argument(
            "--cc", default="", help="CC on every email (addresses or settings tokens)."
        )
        parser.add_argument(
            "--bcc",
            default="",
            help="BCC on every email (addresses or settings tokens).",
        )
        parser.add_argument(
            "--reply-to",
            default="",
            help="Reply-To (default: none, replies go to the sender).",
        )
        parser.add_argument(
            "--no-attachment", action="store_true", help="Do not attach the PDF guide."
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Send to at most N recipients in this run.",
        )
        parser.add_argument("--batch-size", type=int, default=20)
        parser.add_argument(
            "--pause", type=float, default=20.0, help="Seconds to wait between batches."
        )
        parser.add_argument(
            "--max-failures",
            type=int,
            default=10,
            help="Stop after N failures in a row.",
        )
        parser.add_argument(
            "--sample",
            type=int,
            default=5,
            help="Recipients shown (masked) in the dry run.",
        )
        parser.add_argument(
            "--write-preview",
            default="",
            help="Directory for preview.html / preview.txt.",
        )

    def handle(self, *args, **opts):
        campaign = CAMPAIGNS[opts["campaign"]]
        try:
            options = SendOptions(
                cc=parse_addresses(opts["cc"]),
                bcc=parse_addresses(opts["bcc"]),
                reply_to=parse_addresses(opts["reply_to"]),
                attach=not opts["no_attachment"],
                batch_size=max(1, opts["batch_size"]),
                pause_seconds=max(0.0, opts["pause"]),
                limit=max(0, opts["limit"]),
                max_failures=max(1, opts["max_failures"]),
            )
        except AnnouncementError as exc:
            raise CommandError(str(exc)) from exc

        if opts["write_preview"]:
            for path in write_preview(
                campaign, Path(opts["write_preview"]), attach=options.attach
            ):
                self.stdout.write(f"preview written: {path}")
            return

        if opts["test_to"]:
            to = parse_addresses(opts["test_to"])
            if len(to) != 1:
                raise CommandError("--test-to takes exactly one address.")
            send_test(
                campaign,
                to=to[0],
                recipient_name=opts["test_name"],
                options=options,
                include_cc=opts["test_include_cc"],
            )
            copies = (
                len(options.cc) + len(options.bcc) if opts["test_include_cc"] else 0
            )
            self.stdout.write(
                f"TEST sent to {mask_email(to[0])} (+{copies} cc/bcc). Nothing recorded in the sent log."
            )
            return

        report = dry_run_report(campaign, options, sample=opts["sample"])
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
        if not opts["send"]:
            self.stdout.write(
                "DRY RUN: nothing sent. Use --test-to for a single copy, or --send --confirm "
                f"{campaign.slug} to send to all."
            )
            return
        if opts["confirm"] != campaign.slug:
            raise CommandError(f"Refusing to send: pass --confirm {campaign.slug}")
        try:
            result = send_campaign(campaign, options, out=self.stdout.write)
        except AnnouncementError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=2))
        if result["stopped_early"]:
            raise CommandError(
                "Stopped after repeated failures; fix the mail server and re-run (sent ones are skipped)."
            )
