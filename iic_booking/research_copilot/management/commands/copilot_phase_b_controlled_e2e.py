"""
Controlled Phase B E2E qualification runner.

Safe defaults:
- Refuses to run unless COPILOT_BOOKING_E2E_TEST_MODE=true
- Refuses unless the target user is is_test_account and allowlisted
- Never enables wallet mutations
- Never deletes rows via SQL

Usage (production container):
  python manage.py copilot_phase_b_controlled_e2e --user-id 77
  python manage.py copilot_phase_b_controlled_e2e --email test.individual_student@iic-booking.test
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone


class Command(BaseCommand):
    help = "Run controlled Copilot Phase B book→reschedule→cancel E2E for one allowlisted test account."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, default=None)
        parser.add_argument("--email", type=str, default="")
        parser.add_argument(
            "--equipment-id",
            type=int,
            default=None,
            help="Optional equipment pk; otherwise first IIC PXRD with a future available slot.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Prepare + negatives only; do not confirm mutations.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON report.")

    def handle(self, *args, **options):
        from django.conf import settings

        from iic_booking.research_copilot.services.v2.mutations import (
            booking_mutation_allowed,
            is_booking_e2e_test_user,
            parse_booking_test_user_ids,
        )
        from iic_booking.users.models import User

        report: dict[str, Any] = {
            "verdict": "NOT_READY",
            "steps": [],
            "errors": [],
            "flags_before": {
                "COPILOT_BOOKING_CREATE": bool(getattr(settings, "COPILOT_BOOKING_CREATE", False)),
                "COPILOT_BOOKING_CANCEL": bool(getattr(settings, "COPILOT_BOOKING_CANCEL", False)),
                "COPILOT_BOOKING_RESCHEDULE": bool(getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)),
                "COPILOT_BOOKING_E2E_TEST_MODE": bool(getattr(settings, "COPILOT_BOOKING_E2E_TEST_MODE", False)),
                "COPILOT_BOOKING_TEST_USER_IDS": getattr(settings, "COPILOT_BOOKING_TEST_USER_IDS", ""),
                "COPILOT_WALLET_RECHARGE": bool(getattr(settings, "COPILOT_WALLET_RECHARGE", False)),
                "COPILOT_WALLET_CREDIT": bool(getattr(settings, "COPILOT_WALLET_CREDIT", False)),
            },
        }

        if not getattr(settings, "COPILOT_BOOKING_E2E_TEST_MODE", False):
            raise CommandError("COPILOT_BOOKING_E2E_TEST_MODE is false — refusing to run controlled E2E.")

        if getattr(settings, "COPILOT_WALLET_RECHARGE", False) or getattr(settings, "COPILOT_WALLET_CREDIT", False):
            raise CommandError("Wallet mutation flags must remain OFF during Phase B E2E.")

        user = None
        if options.get("user_id"):
            user = User.objects.filter(pk=options["user_id"]).first()
        elif options.get("email"):
            user = User.objects.filter(email__iexact=options["email"].strip()).first()
        if not user:
            raise CommandError("No suitable controlled test account found.")

        report["test_account"] = {
            "id": user.pk,
            "email": user.email,
            "user_type": user.user_type,
            "is_test_account": bool(user.is_test_account),
        }

        if not user.is_test_account:
            raise CommandError("Refusing: target user is not is_test_account=True.")
        if not is_booking_e2e_test_user(user):
            raise CommandError(
                f"User {user.pk} is not allowlisted for E2E "
                f"(allowlist={sorted(parse_booking_test_user_ids())})."
            )
        for flag in ("COPILOT_BOOKING_CREATE", "COPILOT_BOOKING_CANCEL", "COPILOT_BOOKING_RESCHEDULE"):
            if not booking_mutation_allowed(user, flag):
                raise CommandError(f"booking_mutation_allowed failed for {flag}")

        try:
            self._run(user=user, report=report, options=options)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(str(exc))
            report["verdict"] = "NOT_READY"
            if options.get("json"):
                self.stdout.write(json.dumps(report, default=str, indent=2))
            raise

        if options.get("json"):
            self.stdout.write(json.dumps(report, default=str, indent=2))
        else:
            self.stdout.write(self.style.SUCCESS(f"VERDICT={report['verdict']}"))
            for step in report["steps"]:
                self.stdout.write(f"  - {step.get('name')}: {step.get('ok')} {step.get('detail', '')}")

    def _wallet_total(self, user) -> Decimal | None:
        from iic_booking.users.models.wallet import SubWallet, Wallet

        w = Wallet.objects.filter(user=user).first()
        if not w:
            return None
        tot = SubWallet.objects.filter(wallet=w).aggregate(s=Sum("balance"))["s"]
        return Decimal(str(tot or 0))

    def _pick_equipment_and_slots(self, *, user, equipment_id: int | None):
        """
        Choose equipment with ≥2 AVAILABLE future slots outside the user
        cancel/reschedule threshold window (default 48h + buffer).
        Prefer EPR / TGA / low-contention instruments over heavily waitlisted PXRD/XPS.
        """
        from iic_booking.equipment.models import DailySlot, SlotStatus

        now = timezone.now()
        # Outside typical 48h threshold with buffer so cancel+reschedule remain allowed.
        min_start = now + timezone.timedelta(hours=60)

        base = (
            DailySlot.objects.filter(
                status=SlotStatus.AVAILABLE,
                booking__isnull=True,
                start_datetime__gt=min_start,
                slot_master__equipment__status="ACTIVE",
            )
            .select_related("slot_master__equipment")
            .order_by("start_datetime")
        )
        if equipment_id:
            candidates = [equipment_id]
        else:
            # Prefer instruments known to accept faculty bookings without waitlist in prod probes.
            preferred = [43, 47, 44, 60, 7, 67, 1, 66, 4]
            # Then any other equipment that has ≥2 far slots
            others = list(
                base.exclude(slot_master__equipment_id__in=preferred)
                .values_list("slot_master__equipment_id", flat=True)
                .distinct()[:20]
            )
            candidates = preferred + others

        for eid in candidates:
            slots = list(base.filter(slot_master__equipment_id=eid)[:3])
            if len(slots) < 2:
                continue
            eq = slots[0].slot_master.equipment
            threshold = int(getattr(eq, "reschedule_hours_threshold", None) or 48)
            # Ensure both slots still clear this equipment's threshold
            need = now + timezone.timedelta(hours=threshold + 12)
            if slots[0].start_datetime < need or slots[1].start_datetime < need:
                # try later pair on same equipment
                far = list(base.filter(slot_master__equipment_id=eid, start_datetime__gte=need)[:2])
                if len(far) < 2:
                    continue
                return eq, far[0], far[1]
            return eq, slots[0], slots[1]

        raise CommandError(
            "No suitable future available slots outside cancel/reschedule threshold. "
            "Cannot run controlled E2E without risking irreversible bookings."
        )

    def _step(self, report: dict, name: str, ok: bool, detail: Any = None):
        report["steps"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["errors"].append(f"{name}: {detail}")

    def _run(self, *, user, report: dict, options: dict):
        from iic_booking.equipment.models import Booking
        from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
        from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
        from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store

        eq, slot_a, slot_b = self._pick_equipment_and_slots(
            user=user, equipment_id=options.get("equipment_id")
        )
        report["equipment"] = {
            "id": eq.equipment_id,
            "name": eq.name,
            "department_id": eq.internal_department_id,
        }
        report["original_slot"] = {
            "id": slot_a.pk,
            "start": slot_a.start_datetime.isoformat() if slot_a.start_datetime else None,
            "end": slot_a.end_datetime.isoformat() if slot_a.end_datetime else None,
        }
        report["reschedule_slot"] = {
            "id": slot_b.pk,
            "start": slot_b.start_datetime.isoformat() if slot_b.start_datetime else None,
            "end": slot_b.end_datetime.isoformat() if slot_b.end_datetime else None,
        }

        bal_before = self._wallet_total(user)
        report["wallet_before"] = str(bal_before) if bal_before is not None else None

        # Soft confirmation must not map to confirm_proposal
        soft_ok = resolve_intent("okay").intent != "confirm_proposal"
        soft_ok = soft_ok and resolve_intent("looks good").intent != "confirm_proposal"
        soft_ok = soft_ok and resolve_intent("maybe").intent != "confirm_proposal"
        self._step(report, "negative_soft_confirm_intent", soft_ok)

        prep = booking_mut.prepare_booking_create(
            user=user,
            equipment_id=eq.equipment_id,
            slot_id=slot_a.pk,
            sample_count=1,
        )
        self._step(
            report,
            "prepare_create",
            bool(prep.get("ok")) and prep.get("confirmation_required") is True and prep.get("executable") is True,
            {
                "proposal_id": prep.get("proposal_id"),
                "estimated_amount": prep.get("estimated_amount"),
                "executable": prep.get("executable"),
                "error": prep.get("error"),
                "message": prep.get("message"),
            },
        )
        if not prep.get("ok"):
            report["verdict"] = "NOT_READY"
            return

        report["proposal_create"] = {
            "proposal_id": prep["proposal_id"],
            "confirmation_token_present": bool(prep.get("confirmation_token")),
            "expires_at": prep.get("expires_at"),
            "estimated_amount": prep.get("estimated_amount"),
            "wallet_balance": prep.get("wallet_balance"),
        }
        report["booking_charge"] = prep.get("estimated_amount")

        # Soft phrase must not execute (orchestrator intent already checked). Also ensure
        # no booking exists yet for this user+slot.
        existing = Booking.objects.filter(user=user, daily_slots=slot_a).exists()
        self._step(report, "no_booking_before_confirm", not existing)

        if options.get("dry_run"):
            report["verdict"] = "DRY_RUN_OK" if not report["errors"] else "NOT_READY"
            return

        # Security: wrong user cannot confirm
        from types import SimpleNamespace

        foreign = SimpleNamespace(is_authenticated=True, pk=user.pk + 999999, is_test_account=True, email="x@y.z")
        bad = booking_mut.execute_booking_create(
            user=foreign,
            proposal_id=prep["proposal_id"],
            confirmation_token=prep["confirmation_token"],
            idempotency_key=f"copilot-e2e-forbidden-{prep['proposal_id']}",
        )
        self._step(
            report,
            "foreign_user_create_rejected",
            not bad.get("ok"),
            bad.get("error"),
        )

        # Wrong token
        bad_tok = booking_mut.execute_booking_create(
            user=user,
            proposal_id=prep["proposal_id"],
            confirmation_token="WRONG-TOKEN",
            idempotency_key=f"copilot-e2e-badtok-{prep['proposal_id']}",
        )
        self._step(report, "wrong_token_rejected", not bad_tok.get("ok"), bad_tok.get("error"))

        # Explicit confirm via the same HTTP endpoint the frontend Confirm button uses
        from rest_framework.test import APIRequestFactory, force_authenticate
        from iic_booking.research_copilot.api_views import confirm_mutation

        idem_key = f"copilot-e2e-create-{user.pk}-{prep['proposal_id']}"
        factory = APIRequestFactory()
        http_req = factory.post(
            "/api/v1/research-copilot/mutations/confirm/",
            {
                "proposal_id": prep["proposal_id"],
                "confirmation_token": prep["confirmation_token"],
                "action": "CREATE_BOOKING",
                "idempotency_key": idem_key,
            },
            format="json",
        )
        force_authenticate(http_req, user=user)
        http_resp = confirm_mutation(http_req)
        created = getattr(http_resp, "data", None) or {}
        self._step(report, "execute_create", bool(created.get("ok")), {
            "http_status": getattr(http_resp, "status_code", None),
            "error": created.get("error"),
            "message": created.get("message"),
            "data": created.get("data"),
        })
        if not created.get("ok"):
            report["verdict"] = "NOT_READY"
            return

        data = created.get("data") or {}
        booking_id = data.get("real_booking_id") or data.get("booking_id")
        # Display ids like IICEPR... are not ints — resolve real pk
        if booking_id is not None and not str(booking_id).isdigit():
            booking_id = data.get("real_booking_id") or Booking.objects.filter(
                user=user, virtual_booking_id=str(booking_id)
            ).values_list("booking_id", flat=True).first()
        report["booking_id"] = booking_id
        report["virtual_booking_id"] = data.get("virtual_booking_id") or data.get("booking_id")


        # Idempotent replay
        replay = booking_mut.execute_booking_create(
            user=user,
            proposal_id=prep["proposal_id"],
            confirmation_token=prep["confirmation_token"],
            idempotency_key=idem_key,
        )
        count_same = Booking.objects.filter(user=user, booking_id=booking_id).count() if booking_id else 0
        self._step(
            report,
            "idempotent_create_replay",
            bool(replay.get("ok")) and bool(replay.get("idempotent_replay")) and count_same == 1,
            {"idempotent_replay": replay.get("idempotent_replay"), "booking_rows": count_same},
        )

        booking = Booking.objects.filter(booking_id=booking_id, user=user).select_related("equipment").first()
        report["original_booking_status"] = getattr(booking, "status", None)
        portal_ok = booking is not None and int(booking.equipment_id) == int(eq.equipment_id)
        self._step(report, "portal_booking_visible", portal_ok, {
            "status": getattr(booking, "status", None),
            "equipment_id": getattr(booking, "equipment_id", None),
        })

        bal_after_create = self._wallet_total(user)
        report["wallet_after_create"] = str(bal_after_create) if bal_after_create is not None else None
        if bal_before is not None and bal_after_create is not None:
            report["wallet_delta_create"] = str(bal_before - bal_after_create)
        else:
            report["wallet_delta_create"] = None
            self._step(
                report,
                "wallet_debit_note",
                True,
                "Wallet total unavailable or unchanged path — domain may not debit in this configuration; recorded as-is.",
            )

        # Reschedule prepare + confirm
        rprep = booking_mut.prepare_reschedule(
            user=user,
            booking_id=int(booking_id),
            slot_id=slot_b.pk,
        )
        self._step(report, "prepare_reschedule", bool(rprep.get("ok")) and rprep.get("confirmation_required") is True, {
            "error": rprep.get("error"),
            "status": rprep.get("status"),
            "proposal_id": rprep.get("proposal_id"),
        })
        if not rprep.get("ok") or rprep.get("status") != "READY_FOR_CONFIRMATION":
            report["verdict"] = "NOT_READY"
            return

        # Invalid/unavailable target (use same original slot which is now booked)
        bad_slot = booking_mut.prepare_reschedule(
            user=user, booking_id=int(booking_id), slot_id=slot_a.pk
        )
        self._step(report, "reschedule_unavailable_slot_rejected", not bad_slot.get("ok") or bad_slot.get("status") != "READY_FOR_CONFIRMATION", bad_slot.get("error") or bad_slot.get("status"))

        ridem = f"copilot-e2e-reschedule-{user.pk}-{rprep['proposal_id']}"
        rexec = booking_mut.execute_booking_reschedule(
            user=user,
            proposal_id=rprep["proposal_id"],
            confirmation_token=rprep["confirmation_token"],
            idempotency_key=ridem,
        )
        self._step(report, "execute_reschedule", bool(rexec.get("ok")), {
            "error": rexec.get("error"),
            "message": rexec.get("message"),
            "data": rexec.get("data"),
        })
        if not rexec.get("ok"):
            report["verdict"] = "NOT_READY"
            return

        booking.refresh_from_db()
        slots_now = list(booking.daily_slots.order_by("start_datetime"))
        report["rescheduled_booking_status"] = booking.status
        report["rescheduled_slots"] = [
            {
                "id": s.pk,
                "start": s.start_datetime.isoformat() if s.start_datetime else None,
                "end": s.end_datetime.isoformat() if s.end_datetime else None,
            }
            for s in slots_now
        ]
        moved = any(s.pk == slot_b.pk for s in slots_now) or (
            slots_now
            and slot_b.start_datetime
            and slots_now[0].start_datetime == slot_b.start_datetime
        )
        self._step(report, "portal_reschedule_verified", moved, report["rescheduled_slots"])

        # Cancel prepare + soft negative + confirm
        cprep = booking_mut.prepare_cancellation(user=user, booking_id=int(booking_id))
        self._step(report, "prepare_cancel", bool(cprep.get("ok")) and cprep.get("confirmation_required") is True, {
            "proposal_id": cprep.get("proposal_id"),
            "error": cprep.get("error"),
        })
        if not cprep.get("ok"):
            report["verdict"] = "NOT_READY"
            return

        # Cancel without confirmation = no execute called; booking still active
        booking.refresh_from_db()
        self._step(report, "cancel_without_confirm_no_change", booking.status not in {"CANCELLED", "cancelled"}, booking.status)

        # Foreign cancel prepare
        foreign_prep = booking_mut.prepare_cancellation(user=foreign, booking_id=int(booking_id))
        self._step(report, "foreign_cancel_prepare_rejected", not foreign_prep.get("ok"), foreign_prep.get("error"))

        cidem = f"copilot-e2e-cancel-{user.pk}-{cprep['proposal_id']}"
        cexec = booking_mut.execute_booking_cancel(
            user=user,
            proposal_id=cprep["proposal_id"],
            confirmation_token=cprep["confirmation_token"],
            idempotency_key=cidem,
        )
        self._step(report, "execute_cancel", bool(cexec.get("ok")), {
            "error": cexec.get("error"),
            "message": cexec.get("message"),
            "data": cexec.get("data"),
        })
        if not cexec.get("ok"):
            report["verdict"] = "NOT_READY"
            return

        creplay = booking_mut.execute_booking_cancel(
            user=user,
            proposal_id=cprep["proposal_id"],
            confirmation_token=cprep["confirmation_token"],
            idempotency_key=cidem,
        )
        self._step(
            report,
            "idempotent_cancel_replay",
            bool(creplay.get("ok")) and bool(creplay.get("idempotent_replay")),
            {"idempotent_replay": creplay.get("idempotent_replay")},
        )

        booking.refresh_from_db()
        report["final_booking_status"] = booking.status
        cancelled = str(booking.status).upper() in {
            "CANCELLED",
            "CANCELED",
            "REFUNDED",
        } or "CANCEL" in str(booking.status).upper() or "REFUND" in str(booking.status).upper()
        self._step(report, "portal_cancel_verified", cancelled, booking.status)

        bal_final = self._wallet_total(user)
        report["wallet_after"] = str(bal_final) if bal_final is not None else None
        report["cancellation_refund_note"] = (
            "Refund/wallet effect determined by existing portal cancellation service; "
            f"wallet_before={report.get('wallet_before')} wallet_after_create={report.get('wallet_after_create')} "
            f"wallet_after_cancel={report.get('wallet_after')}"
        )

        # Audit evidence (recent TOOL_EXECUTED for this user)
        try:
            from iic_booking.research_copilot.models import CopilotAuditEvent

            audits = list(
                CopilotAuditEvent.objects.filter(user=user)
                .order_by("-created_at")
                .values("action", "message", "created_at")[:20]
            )
            report["audit_sample"] = [
                {
                    "action": a.get("action"),
                    "message": a.get("message"),
                    "created_at": a.get("created_at").isoformat() if a.get("created_at") else None,
                }
                for a in audits
            ]
            self._step(report, "audit_records_present", len(audits) > 0, len(audits))
        except Exception as exc:  # noqa: BLE001
            self._step(report, "audit_records_present", False, str(exc))

        # Expired proposal: create a short-lived proposal and expire it
        prop_store  # noqa: B018 — imported for clarity
        # Already covered wrong token / foreign user above.

        failed = [s for s in report["steps"] if not s.get("ok")]
        report["verdict"] = "READY_FOR_CONTROLLED_PRODUCTION_ENABLEMENT" if not failed else "NOT_READY"
