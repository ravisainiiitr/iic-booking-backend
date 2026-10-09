"""SRIC wallet recharge operations. Output is counts, flags and record ids only (public CI logs)."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "status | dry-run-scan (parse + match only, nothing stored or credited) | enable-scan | disable-scan"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "dry-run-scan", "enable-scan", "disable-scan"])

    def handle(self, *args, **options):
        from django.db.models import Count

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
