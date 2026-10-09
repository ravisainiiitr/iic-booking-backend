"""SRIC wallet recharge operations. Output is counts, flags and record ids only (public CI logs)."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "status | dry-run-scan (parse + match only, nothing stored or credited) | enable-scan | disable-scan | "
        "test-sender-dry-run (one-off test email from --test-sender; nothing stored) | find-test-faculty | "
        "test-sender-e2e (--folder --uid --test-user-id: credit an existing test faculty instead of the file's faculty) | "
        "test-sender-reverse (--row-id: Main Admin ledger debit of a credited test row)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=[
                "status",
                "dry-run-scan",
                "enable-scan",
                "disable-scan",
                "test-sender-dry-run",
                "find-test-faculty",
                "test-sender-e2e",
                "test-sender-reverse",
            ],
        )
        parser.add_argument("--test-sender", default="")
        parser.add_argument("--folder", default="")
        parser.add_argument("--uid", default="")
        parser.add_argument("--test-user-id", default="")
        parser.add_argument("--row-id", default="")
        parser.add_argument("--actor-id", default="")

    @staticmethod
    def _int(value):
        value = str(value or "").strip()
        return int(value) if value.isdigit() else None

    def _test_sender(self, options):
        from iic_booking.users import sric_wallet_test_sender as test_run

        action = options["action"]
        w = self.stdout.write
        try:
            if action == "find-test-faculty":
                rows = test_run.test_faculty_candidates()
                w(f"test_faculty_accounts={len(rows)} eligible={sum(1 for r in rows if r['eligible'])}")
                for r in rows:
                    w(f"  user_id={r['user_id']} eligible={r['eligible']} detail={r['detail']} has_employee_id={r['has_employee_id']} "
                      f"iic_sub_wallet={r['iic_sub_wallet']} iic_balance={r['iic_balance']}")
                return
            if action == "test-sender-reverse":
                r = test_run.reverse(row_id=self._int(options["row_id"]) or 0, actor_id=self._int(options["actor_id"]))
                w(f"reversal row_id={r['row_id']} adjustment={r['adjustment']} created={r['created']} actor_id={r['actor_id']} "
                  f"amount={r['credited_amount']} balance_before_credit={r['balance_before_credit']} "
                  f"balance_before_reversal={r['balance_before_reversal']} balance_after_reversal={r['balance_after_reversal']}")
                return
            result = test_run.run(
                test_sender=options["test_sender"],
                mode="e2e" if action == "test-sender-e2e" else "dry_run",
                folder=options["folder"].strip(),
                uid=options["uid"].strip(),
                test_user_id=self._int(options["test_user_id"]),
                actor_id=self._int(options["actor_id"]),
            )
        except test_run.TestSenderError as exc:
            raise CommandError(str(exc)) from exc
        w(f"mode={result['mode']} test_sender={result['test_sender']} scanner_folder={result['scanner_folder']} "
          f"scanner_reads_test_sender=no cutoff={result['cutoff']}")
        for f in result["folders"]:
            w(f"folder='{f['folder']}' selectable={f['selectable']} from_test_sender={f.get('from_test_sender', '-')}")
        for m in result["messages"]:
            w(f"message folder='{m['folder']}' uid={m['uid']} date={m['date']} in_scanner_folder={m['in_scanner_folder']} "
              f"in_date_window={m['in_date_window']} attachment={m['attachment']} already_stored={m['already_stored']} "
              f"origin_verified_real={m['origin_verified_real']} origin_check_real='{m['origin_check_real']}' "
              f"parse_error='{m.get('parse_error', '')}' rows={len(m.get('rows', []))}")
            for r in m.get("rows", []):
                w(f"  row={r['row']} parse_ok={r['parse_ok']} errors={r['errors']} employee_matched={'yes' if r['employee_matched'] else 'no'} "
                  f"match_detail={r['match_detail']} receiver={r['receiver_code']} -> {r['receiver_label'] or '-'} "
                  f"(department_id={r['department_id']} '{r['department'] or '-'}') amount={r['amount']} ledger={r['ledger_id']} "
                  f"fy={r['financial_year']} duplicate_real={r['duplicate_real_ledger']} would_be={r['would_be']} "
                  f"reason='{r['reason']}' would_credit=no")
        e2e = result.get("e2e")
        if e2e:
            w(f"e2e test_user_id={result['test_user_id']} actor_id={result['actor_id']} message_record_id={e2e['message_record_id']} "
              f"rerun={e2e['rerun']} test_mail_redirects={result['test_mail_redirects']} employee_id_substituted=yes")
            for r in e2e["rows"]:
                w(f"  row_id={r['row_id']} row={r['row']} stored_status={r['stored_status']} reason='{r['reason']}' "
                  f"matched_test_faculty={r['matched_test_faculty']} duplicate_of={r['duplicate_of']} receiver={r['receiver']} "
                  f"amount={r['amount']} ledger={r['ledger_id']} credit={r['credit']} final_status={r['final_status']} "
                  f"wallet_transaction_id={r['wallet_transaction_id']} confirmation_email_sent={r['confirmation_email_sent']} "
                  f"balance_before={r['balance_before']} balance_after={r['balance_after']} in_admin_tab={r['in_admin_tab']} "
                  f"admin_row_is_test={r['admin_row_is_test']} on_faculty_page={r['on_faculty_page']}")

    def handle(self, *args, **options):
        from django.db.models import Count

        if options["action"].startswith("test-sender-") or options["action"] == "find-test-faculty":
            self._test_sender(options)
            return

        from iic_booking.users.models.sric_wallet_recharge import (
            SricReceiverMapping,
            SricWalletMailMessage,
            SricWalletRecharge,
            SricWalletRechargeSettings,
        )
        from iic_booking.users.sric_wallet_recharge import scan_mailbox

        action = options["action"]
        config = SricWalletRechargeSettings.get_singleton()
        if action in ("enable-scan", "disable-scan"):
            config.scan_enabled = action == "enable-scan"
            config.save(update_fields=["scan_enabled", "updated_at"])
        if action == "dry-run-scan":
            result = scan_mailbox(trigger="dry-run", dry_run=True, max_messages=50)
            if result.get("status") in ("error", "not_configured"):
                raise CommandError(f"scan status={result.get('status')} error={result.get('error', '')}")
            for key in ("status", "messages_found", "messages_new", "messages_read", "rows_new", "statuses"):
                self.stdout.write(f"{key}={result.get(key)}")
            for m in result.get("messages", []):
                self.stdout.write(
                    "message uid={uid} date={date} status={status} authenticated={auth} verdict='{verdict}' "
                    "fy={fy} rows={rows} would_be={statuses} reasons={reasons}".format(
                        uid=m.get("uid"),
                        date=m.get("date"),
                        status=m.get("status"),
                        auth=m.get("authenticated"),
                        verdict=m.get("auth_verdict", ""),
                        fy=m.get("financial_year", ""),
                        rows=m.get("rows"),
                        statuses=m.get("statuses"),
                        reasons=m.get("reasons"),
                    )
                )
        from django_celery_beat.models import PeriodicTask

        task = PeriodicTask.objects.filter(task="users.scan_sric_wallet_recharge_mailbox").first()
        self.stdout.write(
            f"settings scan_enabled={config.scan_enabled} auto_credit_enabled={config.auto_credit_enabled} "
            f"require_internal_relay={config.require_internal_relay} gateway_marker_set={bool(config.gateway_marker_header)} "
            f"trusted_authserv_set={bool(config.trusted_authserv_ids.strip())} auto_credit_limit_set={config.auto_credit_max_amount is not None}"
        )
        self.stdout.write(
            f"schedule present={task is not None} enabled={getattr(task, 'enabled', None)} "
            f"interval={getattr(getattr(task, 'interval', None), 'every', None)}{getattr(getattr(task, 'interval', None), 'period', '')} "
            f"last_run_at={getattr(task, 'last_run_at', None)} total_run_count={getattr(task, 'total_run_count', None)}"
        )
        for m in SricReceiverMapping.objects.select_related("department").order_by("code"):
            self.stdout.write(
                f"receiver code={m.code} label={m.label} active={m.is_active} department_id={m.department_id} "
                f"department={m.department.name if m.department else '-'}"
            )
        self.stdout.write(f"last_scan_at={config.last_scan_at} last_scan_result={ {k: v for k, v in (config.last_scan_result or {}).items() if k != 'review_row_ids'} }")
        self.stdout.write(
            f"messages_by_status={dict(SricWalletMailMessage.objects.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))}"
        )
        self.stdout.write(
            f"rows_by_status={dict(SricWalletRecharge.objects.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))}"
        )
