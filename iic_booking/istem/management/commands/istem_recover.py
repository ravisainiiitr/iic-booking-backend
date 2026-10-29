"""Administrator recovery and read-only verification for I-STEM bookings and users.

Each action names the acting administrator (``--actor`` email), who must hold the matching ``istem.*`` permission.
Recovery never sends Create Booking directly; ``mark-not-created`` only re-queues it after a second approver agrees.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from iic_booking.istem import conf, service
from iic_booking.istem.errors import IstemError
from iic_booking.istem.models import IstemBookingLink

ACTIONS = (
    "recover-from-attempt", "adopt-ids", "mark-not-created", "link-user", "verify-confirmation",
    "refresh-status", "request-confirmation", "approve-execute-resubmission", "approve-confirm-resubmission",
    "acknowledge-drift", "verify-equipment", "verify-user",
)


class Command(BaseCommand):
    help = "Resolve uncertain or blocked I-STEM operations (administrators)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=ACTIONS)
        parser.add_argument("--actor", required=True, help="Email of the administrator performing the action.")
        parser.add_argument("--booking", type=int, help="Local booking id.")
        parser.add_argument("--user", help="Email of the local user (link-user, verify-user).")
        parser.add_argument("--equipment", type=int, help="Local equipment id (verify-equipment; default: all active mappings).")
        parser.add_argument("--request-id")
        parser.add_argument("--fbr-no")
        parser.add_argument("--public-user-id")
        parser.add_argument("--approver", help="Email of the second administrator (mark-not-created, approve-*-resubmission).")
        parser.add_argument("--evidence-type", default="")
        parser.add_argument("--evidence-reference", default="")
        parser.add_argument("--reason", default="")

    def _user(self, email):
        try:
            return get_user_model().objects.get(email__iexact=email)
        except get_user_model().DoesNotExist as exc:
            raise CommandError(f"No user with email {email}") from exc

    def _link(self, options):
        if not options["booking"]:
            raise CommandError("--booking is required for this action")
        try:
            return IstemBookingLink.objects.get(booking_id=options["booking"])
        except IstemBookingLink.DoesNotExist as exc:
            raise CommandError(f"Booking {options['booking']} has no I-STEM link") from exc

    def handle(self, *args, **options):
        if conf.istem_mode() == conf.MODE_OFF:
            raise CommandError("The I-STEM integration is off (ISTEM_ENABLED / ISTEM_MODE).")
        actor = self._user(options["actor"])
        action = options["action"]
        evidence = {"evidence_type": options["evidence_type"], "evidence_reference": options["evidence_reference"],
                    "reason": options["reason"]}
        try:
            if action == "recover-from-attempt":
                link = service.recover_create_from_attempt(self._link(options), actor=actor)
                self.stdout.write(f"Recovered: request_id={link.request_id} state={link.sync_state}")
            elif action == "adopt-ids":
                link = service.adopt_remote_ids(self._link(options), request_id=options["request_id"] or "",
                                                fbr_no=options["fbr_no"] or "", actor=actor, **evidence)
                self.stdout.write(f"Adopted: request_id={link.request_id} fbr_no={link.fbr_no}")
            elif action == "mark-not-created":
                if not options["approver"]:
                    raise CommandError("--approver is required")
                job = service.mark_not_created(self._link(options), actor=actor, approver=self._user(options["approver"]), **evidence)
                self.stdout.write(f"Create Booking re-queued as job {job.pk}")
            elif action == "approve-execute-resubmission":
                if not options["approver"]:
                    raise CommandError("--approver is required")
                job = service.approve_execute_resubmission(self._link(options), actor=actor, approver=self._user(options["approver"]), **evidence)
                self.stdout.write(f"FBR Execute re-queued as job {job.pk}")
            elif action == "approve-confirm-resubmission":
                if not options["approver"]:
                    raise CommandError("--approver is required")
                job = service.approve_confirm_resubmission(self._link(options), actor=actor, approver=self._user(options["approver"]), **evidence)
                self.stdout.write(f"Confirm Booking re-queued as job {job.pk}")
            elif action == "acknowledge-drift":
                link = service.acknowledge_drift(self._link(options), actor=actor, **evidence)
                self.stdout.write(f"Drift acknowledged; link is {link.sync_state}")
            elif action == "link-user":
                if not options["user"]:
                    raise CommandError("--user is required")
                user_link = service.link_user_manually(self._user(options["user"]), public_user_id=options["public_user_id"] or "",
                                                       actor=actor, **evidence)
                self.stdout.write(f"User link: {user_link.state}")
            elif action == "request-confirmation":
                job = service.request_confirmation(self._link(options), actor=actor, reason=options["reason"])
                self.stdout.write(f"Confirm Booking queued as job {job.pk}")
            else:
                self._require_reconcile(actor)
                if action == "verify-confirmation":
                    outcome, confirmation_id = service.reconcile_confirmation(self._link(options))
                    self.stdout.write(f"confirmation: {outcome}; confirmation_id: {confirmation_id or 'not found'}")
                elif action == "verify-equipment":
                    ids = [options["equipment"]] if options["equipment"] else None  # default: every active mapping
                    summary = service.verify_equipment_mappings(equipment_ids=ids)
                    self.stdout.write(
                        f"institute_id={summary['institute_id']} user_entity={summary['user_entity'] or '-'} "
                        f"listed={summary['listed']} found={', '.join(summary['found']) or '-'} "
                        f"missing={', '.join(summary['missing']) or '-'}"
                    )
                elif action == "verify-user":
                    if not options["user"]:
                        raise CommandError("--user is required")
                    summary = service.verify_user_link(self._user(options["user"]))
                    lists = {k: ", ".join(v) if v else "-" for k, v in summary.items() if k.startswith("by_") and v is not None}
                    self.stdout.write(
                        f"public_user_id={summary['public_user_id']} "
                        + " ".join(f"{k}={v}" for k, v in lists.items())
                        + f" matches={'yes' if summary['matches'] else 'NO'}"
                    )
                else:
                    self.stdout.write(f"I-STEM status: {service.refresh_remote_status(self._link(options)) or 'not found'}")
        except PermissionError as exc:
            raise CommandError(f"Permission denied: {exc}") from exc
        except (ValueError, IstemError) as exc:
            raise CommandError(str(exc)) from exc

    @staticmethod
    def _require_reconcile(actor):
        if not actor.is_active or not actor.has_perm("istem.reconcile"):
            raise PermissionError("istem.reconcile is required")
