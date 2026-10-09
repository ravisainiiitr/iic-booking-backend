"""Wallet ledger: students linked to a wallet owner (Main Administrator)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from iic_booking.users.models import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.test_admin_wallet_ledger import OWNERS, TXNS, LedgerBase, User

STUDENTS = "/api/admin/wallet-ledger/linked-students/"


class LinkedStudentsBase(LedgerBase):
    def setUp(self):
        super().setUp()
        self.link = WalletJoinRequest.objects.get(student=self.student, faculty=self.fac)
        WalletJoinRequest.objects.filter(pk=self.link.pk).update(responded_at=timezone.now() - timedelta(days=40))
        self.sw1.credit(Decimal("100.00"), "Refund for cancelled Booking IIC-XRD-0002 | Ref: IIC-XRD-0002", related_user=self.student)
        self.sw1.credit(Decimal("500.00"), "Wallet recharge approved — WRR-5", related_user=self.student)

    def make_student(self, name, status, *, responded=True, limits=False, emp_id=None):
        user = User.objects.create_user(
            email=f"{name.lower().replace(' ', '.')}@test.iitr.ac.in", password="x12345678", name=name,
            user_type=UserType.STUDENT, department=self.phys, emp_id=emp_id,
        )
        link = WalletJoinRequest.objects.create(student=user, faculty=self.fac, wallet=self.w1, status=status)
        fields = {}
        if responded:
            fields["responded_at"] = timezone.now() - timedelta(days=3)
        if limits:
            fields.update(spending_limit_enabled=True, weekly_limit_inr=Decimal("1000.00"), monthly_limit_inr=Decimal("3000.00"))
        if fields:
            WalletJoinRequest.objects.filter(pk=link.pk).update(**fields)
        return user

    def get(self, client=None, **params):
        res = (client or self.client).get(STUDENTS, {"owner": self.fac.pk, **params})
        return res

    def names(self, **params):
        res = self.get(**params)
        self.assertEqual(res.status_code, 200, res.content[:200])
        names = dict(User.objects.values_list("pk", "name"))
        return [names[r["student_id"]] for r in res.data["results"]]


class LinkedStudentsTests(LinkedStudentsBase):
    def test_main_admin_only(self):
        for user in (self.fac, self.student):
            self.assertEqual(self.get(self.as_user(user)).status_code, 403)
        self.assertEqual(self.client.get(STUDENTS, {"owner": self.admin.pk}).status_code, 404)

    def test_link_statuses_spend_and_sub_wallets(self):
        self.make_student("Pending Pat", WalletJoinRequestStatus.PENDING, responded=False)
        self.make_student("Removed Rui", WalletJoinRequestStatus.CANCELLED)
        self.make_student("Withdrawn Wen", WalletJoinRequestStatus.CANCELLED, responded=False)
        self.make_student("Declined Dev", WalletJoinRequestStatus.REJECTED)
        limited = self.make_student("Limited Lee", WalletJoinRequestStatus.APPROVED, limits=True, emp_id="21100")
        data = self.get().data
        by_name = {r["name"]: r for r in data["results"]}
        self.assertEqual(
            {r["status"] for r in data["results"]}, {"linked", "pending", "removed", "cancelled", "declined"}
        )
        self.assertEqual(data["summary"]["linked"], 2)
        self.assertEqual(data["summary"]["pending"], 1)
        self.assertEqual(data["summary"]["with_limits"], 1)
        stud = next(r for r in data["results"] if r["student_id"] == self.student.pk)
        self.assertEqual(stud["total_charged"], "300.00")
        self.assertEqual(stud["total_refunded"], "100.00")
        self.assertEqual(stud["total_spent"], "200.00")
        self.assertEqual([s["department_name"] for s in stud["sub_wallets"]], ["Chemistry WL"])
        self.assertIsNotNone(stud["last_charge_at"])
        self.assertFalse(stud["spending_limit_enabled"])
        lee = next(r for r in data["results"] if r["student_id"] == limited.pk)
        self.assertTrue(lee["spending_limit_enabled"])
        self.assertEqual((lee["weekly_limit"], lee["monthly_limit"]), ("1000.00", "3000.00"))
        self.assertEqual((lee["week_spent"], lee["month_spent"]), ("0.00", "0.00"))
        self.assertEqual(lee["enrollment"], "21100")
        self.assertIsNone(by_name["Pending Pat"]["week_spent"])
        self.assertEqual(data["results"][0]["s_no"], 1)
        self.assertEqual(data["results"][0]["status"], "linked")

    def test_one_row_per_student_prefers_approved(self):
        WalletJoinRequest.objects.create(
            student=self.student, faculty=self.fac, wallet=self.w1, status=WalletJoinRequestStatus.PENDING
        )
        rows = [r for r in self.get().data["results"] if r["student_id"] == self.student.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "linked")

    def test_filters_search_sort_and_range(self):
        self.make_student("Pending Pat", WalletJoinRequestStatus.PENDING, responded=False)
        self.make_student("Limited Lee", WalletJoinRequestStatus.APPROVED, limits=True, emp_id="21100")
        self.assertEqual(self.names(status="linked", ordering="name"), ["Limited Lee", "Stud WL"])
        self.assertEqual(self.names(status="pending"), ["Pending Pat"])
        self.assertEqual(self.names(search="2110"), ["Limited Lee"])
        self.assertEqual(self.names(ordering="-total_spent", status="linked")[0], "Stud WL")
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        data = self.get(date_from=tomorrow).data
        stud = next(r for r in data["results"] if r["student_id"] == self.student.pk)
        self.assertEqual((stud["total_spent"], stud["range_spent"]), ("200.00", "0.00"))
        today = timezone.localdate().isoformat()
        stud = next(r for r in self.get(date_from=today, date_to=today).data["results"] if r["student_id"] == self.student.pk)
        self.assertEqual(stud["range_spent"], "200.00")

    def test_supervised_users_listed_separately(self):
        pdf = User.objects.create_user(
            email="pdf.lo@test.iitr.ac.in", password="x12345678", name="Post Doc", user_type=UserType.STUDENT,
            supervisor=self.fac,
        )
        User.objects.filter(pk=self.student.pk).update(supervisor=self.fac)
        data = self.get().data
        self.assertEqual([r["student_id"] for r in data["supervised"]], [pdf.pk])
        self.assertEqual(data["summary"]["supervised"], 1)
        self.assertNotIn(pdf.pk, [r["student_id"] for r in data["results"]])

    def test_query_count_does_not_grow_with_students(self):
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.get().status_code, 200)
        for i in range(4):
            self.make_student(f"Extra {i}", WalletJoinRequestStatus.APPROVED, limits=bool(i % 2))
            self.make_student(f"Waiting {i}", WalletJoinRequestStatus.PENDING, responded=False)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.get().data["summary"]["linked"], 5)
        self.assertEqual(len(large.captured_queries), len(small.captured_queries))
        self.assertLessEqual(len(large.captured_queries), 12)


class OwnerAndTransactionFilterTests(LinkedStudentsBase):
    def test_owner_has_students_filter(self):
        yes = {r["owner_id"] for r in self.client.get(OWNERS, {"has_students": "yes"}).data["results"]}
        no = {r["owner_id"] for r in self.client.get(OWNERS, {"has_students": "no"}).data["results"]}
        self.assertEqual(yes, {self.fac.pk})
        self.assertEqual(no, {self.fac2.pk, self.ext.pk})
        ordered = [r["owner_id"] for r in self.client.get(OWNERS, {"ordering": "-students"}).data["results"]]
        self.assertEqual(ordered[0], self.fac.pk)

    def test_transactions_filtered_by_booking_user(self):
        res = self.client.get(TXNS, {"owner": self.fac.pk, "related_user": self.student.pk})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 3)
        self.assertTrue(all(r["related_user_name"] for r in res.data["results"]))

    def test_export(self):
        self.make_student("Limited Lee", WalletJoinRequestStatus.APPROVED, limits=True)
        res = self.client.get(
            "/api/exports/admin-wallet-linked-students/", {"export_format": "csv", "owner": self.fac.pk, "status": "linked"}
        )
        self.assertEqual(res.status_code, 200)
        text = res.content.decode("utf-8-sig")
        self.assertIn("Limited Lee", text)
        self.assertIn("Week ₹0.00 of ₹1000.00", text)
        self.assertNotIn(self.student.email, text)
        self.assertEqual(
            self.client.get(
                "/api/exports/admin-wallet-linked-students/", {"export_format": "xlsx", "owner": self.fac.pk}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.as_user(self.fac).get(
                "/api/exports/admin-wallet-linked-students/", {"export_format": "csv", "owner": self.fac.pk}
            ).status_code,
            403,
        )
