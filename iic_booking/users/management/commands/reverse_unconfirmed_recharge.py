"""Reverse Project Grant recharges approved only by the requester / wallet owner (no SRIC confirmation).

Dry run by default. --apply needs --confirm "REVERSE <txn>,<txn>" naming exactly the --txn values, and
reverses nothing unless every named request passes all checks. Output is ids, flags and amounts only
(public CI logs).
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Reverse unconfirmed (self-approved) Project Grant recharges: --txn IIC-TXN-000049 [--txn …] [--apply --confirm …]"

    def add_arguments(self, parser):
        parser.add_argument("--txn", action="append", required=True, help="IIC-TXN-###### (repeatable)")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm", default="")
        parser.add_argument("--actor-id", type=int, default=None, help="Main Administrator user id (default: first superuser)")
        parser.add_argument("--no-notice", action="store_true", help="Reverse without emailing / notifying the user")
        parser.add_argument("--preview-notice", action="store_true", help="Print the email text with the name masked")

    def handle(self, *args, **options):
        from iic_booking.users import recharge_reversal as rr
        from iic_booking.users.models.wallet import WalletRechargeRequest

        w = self.stdout.write
        txns = []
        for value in options["txn"]:
            for part in value.split(","):
                if part.strip() and part.strip().upper() not in txns:
                    txns.append(part.strip().upper())
        try:
            pks = [rr.parse_txn(t) for t in txns]
        except rr.ReversalError as exc:
            raise CommandError(str(exc)) from exc
        requests = {
            r.pk: r
            for r in WalletRechargeRequest.objects.select_related("user", "wallet__user", "department").filter(pk__in=pks)
        }
        missing = [t for t, pk in zip(txns, pks, strict=True) if pk not in requests]
        if missing:
            raise CommandError(f"Not found: {', '.join(missing)}")

        expected_confirm = f"REVERSE {','.join(txns)}"
        apply = options["apply"]
        if apply and options["confirm"].strip() != expected_confirm:
            raise CommandError(f'--apply needs --confirm "{expected_confirm}"')
        actor = rr.main_admin(options["actor_id"])
        w(f"mode={'apply' if apply else 'dry-run'} actor_id={actor.pk} notice={'no' if options['no_notice'] else 'yes'}")

        plans = []
        for pk in pks:
            req = requests[pk]
            facts = rr.assess(req)
            plans.append((req, facts))
            w(
                f"{facts['txn']} request_id={pk} status={facts['status']} mode={facts['mode']} amount={facts['amount']} "
                f"owner_id={facts['owner_id']} requester_id={facts['requester_id']} department_id={facts['department_id']} "
                f"approval_source={facts['approval_source']} cashbook_matched={facts['cashbook_matched']} "
                f"fund_receipt_verified={facts['fund_receipt_verified']} wallet_credited_at={facts['wallet_credited_at']}"
            )
            w(
                f"  sub_wallet_id={facts.get('sub_wallet_id')} credit_entry_ids={facts.get('credit_entry_ids')} "
                f"debits_since_credit={facts.get('debits_since_credit')} credits_since_credit={facts.get('credits_since_credit')} "
                f"balance_before={facts.get('balance_before')} balance_after={facts.get('balance_after')} "
                f"already_reversed={facts['already_reversed']} existing_reversal={facts['existing_reversal'] or '-'}"
            )
            recipients = rr.notice_recipients(req)
            roles = ["owner" if u.pk == req.wallet.user_id else "requester" for u in recipients]
            w(f"  notice_recipients={roles} eligible={facts['eligible']} blockers={facts['blockers'] or '-'}")
            if facts["eligible"]:
                w(
                    f"  plan: debit {facts['amount']} from sub_wallet {facts['sub_wallet_id']} (WalletAdminAdjustment, "
                    f"client_request_id={rr.client_request_id(req)}, reason=correction, external_reference={facts['txn']}); "
                    f"request APPROVED -> CANCELLED (cancellation_source=admin); audit '{rr.AUDIT_ACTION}'; remarks='{rr.REVERSAL_REASON}'"
                )
            if options["preview_notice"]:
                content = rr.notice_content(
                    req,
                    recipient_name="<name>",
                    reference="WAD-YYYY-NNNNNN",
                    balance_after=facts.get("balance_after"),
                    credited_on=facts.get("credited_on") or req.wallet_credited_at,
                    reversed_on=None,
                )
                w(f"  notice_subject={content['subject']}")
                w("  ---- notice text ----")
                for line in content["text"].splitlines():
                    w(f"  | {line}")
                w("  ---- end ----")

        pending = [(r, f) for r, f in plans if not f["already_reversed"]]
        blocked = [f["txn"] for _, f in pending if not f["eligible"]]
        if blocked:
            w(f"BLOCKED={blocked} — nothing is reversed while any named request fails a check")
            if apply:
                raise CommandError(f"Refused: {', '.join(blocked)} failed checks; nothing was changed.")
            return
        if not apply:
            w(f"dry-run complete; to apply: --apply --confirm \"{expected_confirm}\"")
            return

        for req, facts in plans:
            if not facts["already_reversed"]:
                try:
                    result = rr.reverse(req, actor=actor)
                except rr.ReversalError as exc:
                    raise CommandError(str(exc)) from exc
                w(
                    f"REVERSED {result['txn']} adjustment={result['adjustment_reference']} adjustment_id={result['adjustment_id']} "
                    f"sub_wallet_transaction_id={result['sub_wallet_transaction_id']} balance_before={result['balance_before']} "
                    f"balance_after={result['balance_after']}"
                )
            else:
                w(f"ALREADY_REVERSED {facts['txn']} adjustment={facts['existing_reversal']}")
            if not options["no_notice"]:
                notice = rr.send_notice(req, actor=actor)
                w(f"  notice {facts['txn']} sent={notice['sent']} already_sent={notice['already_sent']} roles={notice.get('roles', [])} in_app={notice.get('in_app', '-')}")
            req.refresh_from_db()
            w(f"  now status={req.status} cancellation_source={req.cancellation_source} audit={list(req.audit_logs.order_by('created_at').values_list('action', flat=True))}")
