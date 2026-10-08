"""Legacy manual wallet credit / debit paths are off; balances change only through the Wallet ledger."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import NoReverseMatch, reverse
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction

User = get_user_model()


class LegacyAdjustmentsOffTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Chemistry LO", code="CHLO", department_type=DepartmentType.INTERNAL)
        self.dept2 = Department.objects.create(name="Physics LO", code="PHLO", department_type=DepartmentType.INTERNAL)
        self.admin = User.objects.create_superuser(email="admin.lo@test.iitr.ac.in", password="x12345678")
        User.objects.filter(pk=self.admin.pk).update(user_type=UserType.ADMIN, name="Admin LO")
        self.admin.refresh_from_db()
        self.owner = User.objects.create_user(
            email="owner.lo@test.iitr.ac.in", password="x12345678", name="Owner LO", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.wallet = Wallet.objects.create(user=self.owner)
        self.sub = SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("0.00"))
        self.txn = self.sub.credit(Decimal("500.00"), "Wallet recharge approved — WRR-9")
        self.empty = SubWallet.objects.create(wallet=self.wallet, department=self.dept2, balance=Decimal("0.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def _balance(self):
        return SubWallet.objects.get(pk=self.sub.pk).balance

    def test_admin_api_credit_and_debit_are_gone(self):
        for action in ("credit", "debit"):
            res = self.api.post(f"/api/admin/sub-wallets/{self.sub.pk}/{action}/", {"amount": "10.00"}, format="json")
            self.assertEqual(res.status_code, 410, action)
            self.assertEqual(res.json()["code"], "USE_WALLET_LEDGER")
            self.assertIn("Wallet ledger", res.json()["error"])
        self.assertEqual(self._balance(), Decimal("500.00"))
        self.assertEqual(SubWalletTransaction.objects.filter(sub_wallet=self.sub).count(), 1)

    def test_admin_api_transaction_delete_is_gone(self):
        res = self.api.delete(f"/api/admin/sub-wallet-transactions/{self.txn.pk}/")
        self.assertEqual(res.status_code, 410)
        self.assertTrue(SubWalletTransaction.objects.filter(pk=self.txn.pk).exists())
        self.assertEqual(self._balance(), Decimal("500.00"))
        self.assertEqual(self.api.get(f"/api/admin/sub-wallet-transactions/{self.txn.pk}/").status_code, 200)

    def test_admin_api_sub_wallet_delete_needs_zero_balance(self):
        res = self.api.delete(f"/api/admin/sub-wallets/{self.sub.pk}/")
        self.assertEqual(res.status_code, 409)
        self.assertTrue(SubWallet.objects.filter(pk=self.sub.pk).exists())
        self.assertEqual(self.api.delete(f"/api/admin/sub-wallets/{self.empty.pk}/").status_code, 204)

    def test_admin_api_sub_wallet_update_cannot_change_balance(self):
        res = self.api.patch(f"/api/admin/sub-wallets/{self.sub.pk}/", {"balance": "99999.00"}, format="json")
        self.assertIn(res.status_code, (200, 400, 405))
        self.assertEqual(self._balance(), Decimal("500.00"))

    def test_django_admin_has_no_credit_debit_actions_or_urls(self):
        model_admin = site._registry[SubWallet]
        request = RequestFactory().get("/admin/users/subwallet/")
        request.user = self.admin
        actions = model_admin.get_actions(request)
        self.assertNotIn("credit_selected", actions)
        self.assertNotIn("debit_selected", actions)
        for name in ("admin:users_subwallet_credit", "admin:users_subwallet_debit"):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[self.sub.pk])
        self.assertIn("Wallet ledger", str(model_admin.ledger_link(self.sub)))

    def test_django_admin_sub_wallet_money_fields_read_only(self):
        model_admin = site._registry[SubWallet]
        request = RequestFactory().get("/")
        request.user = self.admin
        readonly = model_admin.get_readonly_fields(request, self.sub)
        for field in ("wallet", "department", "balance_display"):
            self.assertIn(field, readonly)
        self.assertNotIn("balance", [f for fs in model_admin.get_fieldsets(request, self.sub) for f in fs[1]["fields"]])
        self.assertFalse(model_admin.has_delete_permission(request, self.sub))
        self.assertTrue(model_admin.has_delete_permission(request, self.empty))
        self.assertTrue(site._registry[Wallet].get_readonly_fields(request, self.wallet).count("user"))

    def test_django_admin_change_post_cannot_move_sub_wallet(self):
        other = User.objects.create_user(
            email="other.lo@test.iitr.ac.in", password="x12345678", name="Other LO", user_type=UserType.FACULTY,
        )
        other_wallet = Wallet.objects.create(user=other)
        self.client.force_login(self.admin)
        changelist = self.client.get(reverse("admin:users_subwallet_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, "/admin/wallet-ledger/")
        self.assertNotContains(changelist, "Credit selected sub-wallets")
        url = reverse("admin:users_subwallet_change", args=[self.sub.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"wallet": other_wallet.pk, "department": self.dept2.pk, "_save": "Save"})
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.wallet_id, self.wallet.pk)
        self.assertEqual(self.sub.department_id, self.dept.pk)
        self.assertEqual(self.sub.balance, Decimal("500.00"))

    def test_django_admin_transactions_cannot_be_deleted(self):
        model_admin = site._registry[SubWalletTransaction]
        request = RequestFactory().get("/")
        request.user = self.admin
        self.assertFalse(model_admin.has_delete_permission(request, self.txn))
        self.assertFalse(model_admin.has_change_permission(request, self.txn))
        self.assertFalse(model_admin.has_add_permission(request))

    def test_user_flows_still_move_money(self):
        """The model-level credit / debit used by recharges, bookings and refunds is untouched."""
        self.sub.debit(Decimal("100.00"), "Booking #1 - XRD | Ref: IIC-XRD-0001")
        self.sub.credit(Decimal("40.00"), "Refund for cancelled Booking IIC-XRD-0001")
        self.assertEqual(self._balance(), Decimal("440.00"))
