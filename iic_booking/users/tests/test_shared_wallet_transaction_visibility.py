"""Internal IITR students on a supervisor's wallet see every transaction of that wallet, as the owner does."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models import SubWalletTransaction
from iic_booking.users.models.wallet import SubWallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.wallet_visibility import sees_all_wallet_transactions

User = get_user_model()
T = SubWalletTransaction.TransactionType


class SharedWalletTransactionVisibilityTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Shared Vis Dept", code="SVD", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="prof.sharedvis@test.iitr.ac.in", password="pass12345", name="Prof SV",
            user_type=UserType.FACULTY, department=self.dept,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        self.sub = SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("470.00"))
        self.student_a = self._member("a.sharedvis@test.iitr.ac.in", UserType.STUDENT)
        self.student_b = self._member("b.sharedvis@test.iitr.ac.in", UserType.STUDENT)
        self.other = self._member("other.sharedvis@example.com", UserType.OTHER)
        for desc, kind, amount, who in (
            ("recharge", T.CREDIT, 600, self.faculty),
            ("faculty booking", T.DEBIT, 50, self.faculty),
            ("student A booking", T.DEBIT, 40, self.student_a),
            ("student B booking", T.DEBIT, 30, self.student_b),
            ("other booking", T.DEBIT, 10, self.other),
        ):
            SubWalletTransaction.objects.create(
                sub_wallet=self.sub, transaction_type=kind, amount=amount, description=desc, related_user=who,
            )

    def _member(self, email, user_type):
        user = User.objects.create_user(
            email=email, password="pass12345", name="Member SV", user_type=user_type, department=self.dept,
        )
        WalletJoinRequest.objects.create(
            student=user, faculty=self.faculty, wallet=self.wallet, status=WalletJoinRequestStatus.APPROVED,
        )
        return user

    def _descriptions(self, user, url):
        client = APIClient()
        client.force_authenticate(user)
        res = client.get(url, {"limit": 100})
        self.assertEqual(res.status_code, 200, res.content)
        return {row["description"] for row in res.data["transactions"]}

    def _both_lists(self, user):
        return (
            self._descriptions(user, "/api/wallet/transactions/"),
            self._descriptions(user, f"/api/wallet/sub-wallets/{self.dept.pk}/transactions/"),
        )

    def test_students_on_the_wallet_see_every_transaction(self):
        everything = {"recharge", "faculty booking", "student A booking", "student B booking", "other booking"}
        for user in (self.faculty, self.student_a, self.student_b):
            wallet_page, sub_wallet = self._both_lists(user)
            self.assertEqual(wallet_page, everything, user.email)
            self.assertEqual(sub_wallet, everything, user.email)

    def test_non_student_member_keeps_own_debits_plus_credits(self):
        wallet_page, sub_wallet = self._both_lists(self.other)
        self.assertEqual(wallet_page, {"recharge", "other booking"})
        self.assertEqual(sub_wallet, {"recharge", "other booking"})

    def test_rule(self):
        self.assertTrue(sees_all_wallet_transactions(self.faculty, self.wallet))
        self.assertTrue(sees_all_wallet_transactions(self.student_a, self.wallet))
        self.assertFalse(sees_all_wallet_transactions(self.other, self.wallet))
        self.assertFalse(sees_all_wallet_transactions(None, self.wallet))
