"""SRIC mailbox readers: 5-minute schedule, Refresh (Main Admin / Accounts In-charge) with lock and cooldown."""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from rest_framework.test import APIClient

from iic_booking.users import sric_mailbox_sync as sync
from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import UserType
from iic_booking.users.models.sric_wallet_recharge import SricWalletRechargeSettings
from iic_booking.users.tests.test_sric_wallet_recharge import User

SRIC_OK = {"status": "ok", "messages_read": 2, "rows_new": 2, "credited": 1, "statuses": {"credited": 1, "needs_review": 1}}
CASH_OK = {"status": "ok", "messages_read": 1, "rows_stored": 3, "requests_matched": 1, "errors": []}


def _user(email: str, user_type: str) -> User:
    return User.objects.create_user(
        email=email, password="pass12345", email_verified=True, admin_approved=True, name=email.split("@")[0], user_type=user_type
    )


class ScheduleTests(TestCase):
    def setUp(self):
        minutes, _ = IntervalSchedule.objects.get_or_create(every=30, period=IntervalSchedule.MINUTES)
        for name in (sync.SRIC_CSV_TASK, sync.CASHBOOK_TASK):
            PeriodicTask.objects.update_or_create(task=name, defaults={"name": f"test {name}", "interval": minutes, "enabled": True})

    def test_sync_schedule_puts_both_readers_on_the_poll_interval(self):
        dry = sync.sync_schedules(apply=False)
        self.assertEqual({r["before_seconds"] for r in dry}, {1800})
        self.assertFalse(any(r["changed"] for r in dry))
        out = StringIO()
        call_command("sric_wallet_recharge", "sync-schedule", stdout=out)
        for name in (sync.SRIC_CSV_TASK, sync.CASHBOOK_TASK):
            task = PeriodicTask.objects.get(task=name)
            self.assertEqual((task.interval.every, task.interval.period), (300, IntervalSchedule.SECONDS))
        self.assertIn("target_seconds=300", out.getvalue())
        self.assertFalse(any(r["changed"] for r in sync.sync_schedules(apply=True)))

    @override_settings(SRIC_MAILBOX_POLL_SECONDS=120)
    def test_interval_is_configurable(self):
        sync.sync_schedules(apply=True)
        self.assertEqual(PeriodicTask.objects.get(task=sync.CASHBOOK_TASK).interval.every, 120)


class RefreshApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = _user("main.admin@test.iitr.ac.in", UserType.ADMIN)
        self.finance = _user("finance@test.iitr.ac.in", UserType.FINANCE)
        self.faculty = _user("fac@test.iitr.ac.in", UserType.FACULTY)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_only_main_admin_and_finance(self):
        self.assertEqual(self.client_for(self.faculty).get("/api/admin/sric-mailbox/status/").status_code, 403)
        self.assertEqual(self.client_for(self.faculty).post("/api/admin/sric-mailbox/refresh/").status_code, 403)
        self.assertEqual(self.client_for(self.finance).get("/api/admin/sric-mailbox/status/").status_code, 200)

    def test_refresh_reads_both_mailboxes_then_cools_down(self):
        with mock.patch.object(svc, "scan_mailbox", return_value=SRIC_OK) as scan, mock.patch(
            "iic_booking.users.wallet_cashbook_mailbox.read_cashbook_mailbox", return_value=CASH_OK
        ) as cash:
            first = self.client_for(self.finance).post("/api/admin/sric-mailbox/refresh/")
            second = self.client_for(self.admin).post("/api/admin/sric-mailbox/refresh/")
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["result"]["totals"], {"new_emails": 3, "rows_parsed": 5, "matched": 2, "unmatched": 3})
        self.assertIn("3 new email(s)", body["result"]["message"])
        self.assertEqual(scan.call_args.kwargs["trigger"], "admin-refresh")
        self.assertEqual(cash.call_count, 1)
        self.assertIn("next_check_at", body["status"])
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "COOLDOWN")
        self.assertGreater(int(second["Retry-After"]), 0)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.client_for(self.admin).get("/api/admin/sric-mailbox/status/").json()["last_refresh"]["by_user_id"], self.finance.pk)

    def test_scan_in_progress_is_not_run_twice(self):
        config = SricWalletRechargeSettings.get_singleton()
        config.scan_enabled = True
        config.quiet_window_enabled = False
        config.save()
        cache.add(svc.LOCK_KEY, "1", 60)
        with mock.patch.object(svc, "_imap_config", return_value={"host": "x"}), mock.patch.object(svc, "_scan") as scan, mock.patch(
            "iic_booking.users.wallet_cashbook_mailbox.read_cashbook_mailbox", return_value={"status": "already_running"}
        ):
            body = self.client_for(self.admin).post("/api/admin/sric-mailbox/refresh/").json()
        scan.assert_not_called()
        self.assertEqual(body["result"]["readers"]["sric_csv"]["status"], "already_running")
        self.assertIn("already running", body["result"]["message"])
        self.assertTrue(body["status"]["running"])

    def test_cashbook_reader_records_its_last_run(self):
        from iic_booking.users import wallet_cashbook_mailbox as cashbook
        from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

        s = WalletSricSettings.get_singleton()
        s.auto_read_cashbook_mailbox = True
        s.save()
        with mock.patch.object(cashbook, "_imap_config", return_value={"host": "x"}), mock.patch.object(
            cashbook, "_read", return_value={"status": "ok", "messages_read": 0, "rows_stored": 0, "requests_matched": 0, "errors": []}
        ):
            cashbook.read_cashbook_mailbox(trigger="refresh")
        last = cashbook.last_run()
        self.assertEqual((last["trigger"], last["status"]), ("refresh", "ok"))
        self.assertTrue(sync.status()["readers"][1]["last_checked_at"])
