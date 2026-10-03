"""Per-department wallet payment options, email recipients and direct wallet recharge."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import ProgrammingError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users import wallet_payment_modes as svc
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    WalletPeerTransfer,
    WalletRechargeMode,
    WalletRechargeRequest,
)
from iic_booking.users.models.wallet_credit_facility import WalletCreditPolicy
from iic_booking.users.models.wallet_payment_modes import (
    WalletDirectRecharge,
    WalletDirectRechargeGrant,
    WalletModeDepartmentSetting,
    WalletModeEmailRecipients,
    WalletPaymentModeAuditEvent,
    WalletPaymentModeConfig,
)
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings, wallet_mode_flags
from iic_booking.users.wallet_credit_facility_v2 import WalletCreditError, create_and_submit_request
from iic_booking.users.wallet_peer_transfer import peer_transfer_staff_emails
from iic_booking.users.wallet_recharge_workflow import (
    get_recharge_approver_emails,
    get_recharge_cc_emails,
    send_sric_approval_email,
)

User = get_user_model()

SEND_OTP = "/api/wallet/recharge-request/send-otp/"
GATEWAY_ORDER = "/api/payments/razorpay/create-order/"
LEGACY_GATEWAY_ORDER = "/api/wallet/razorpay/create-order/"
PEER_SEND_OTP = "/api/wallet/peer-transfer/send-otp/"
PEER_CONFIRM = "/api/wallet/peer-transfer/confirm/"
SETTINGS = "/api/wallet/student-recharge/settings/"
OVERVIEW = "/api/admin/wallet-payment-modes/"
DEPARTMENTS = "/api/admin/wallet-payment-modes/departments/"
RECIPIENTS = "/api/admin/wallet-payment-modes/recipients/"
GRANTS = "/api/admin/wallet-direct-recharge/grants/"
ACCESS = "/api/wallet/direct-recharge/access/"
PREVIEW = "/api/wallet/direct-recharge/preview/"
RECHARGE = "/api/wallet/direct-recharge/"
HISTORY = "/api/wallet/direct-recharge/history/"
MODES = "/api/admin/wallet-mode-settings/"


class Base(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Chem Modes", code="CHM", department_type=DepartmentType.INTERNAL)
        self.other = Department.objects.create(name="Phys Modes", code="PHM", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="fac.pm@test.iitr.ac.in", password="pass12345", name="Fac PM", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.colleague = User.objects.create_user(
            email="col.pm@test.iitr.ac.in", password="pass12345", name="Col PM", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.admin = User.objects.create_user(
            email="admin.pm@test.iitr.ac.in", password="pass12345", name="Admin PM", user_type=UserType.ADMIN,
        )
        self.designee = User.objects.create_user(
            email="desig.pm@test.iitr.ac.in", password="pass12345", name="Desig PM", user_type=UserType.FINANCE,
            department=self.dept, admin_approved=True,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        self.sub = SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("100.00"))
        SubWallet.objects.create(wallet=self.wallet, department=self.other, balance=Decimal("0"))
        self.api = APIClient()
        self.api.force_authenticate(self.faculty)
        self.admin_api = APIClient()
        self.admin_api.force_authenticate(self.admin)

    def masters(self, **flags):
        WalletSricSettings.get_singleton()
        WalletSricSettings.objects.filter(pk=1).update(**flags)

    def disable(self, dept, *options):
        row, _ = WalletModeDepartmentSetting.objects.get_or_create(department=dept)
        for option in options:
            setattr(row, option, "disabled")
        row.save()


class EffectiveSettingTests(Base):
    def test_master_off_is_off_everywhere(self):
        self.masters(direct_cash_recharge_enabled=False)
        self.assertFalse(svc.option_enabled("direct_cash", self.dept))
        self.assertFalse(svc.option_enabled("direct_cash"))

    def test_master_on_department_inherits_or_disables(self):
        self.masters(direct_cash_recharge_enabled=True)
        self.assertTrue(svc.option_enabled("direct_cash", self.dept))
        self.disable(self.dept, "direct_cash")
        self.assertFalse(svc.option_enabled("direct_cash", self.dept))
        self.assertTrue(svc.option_enabled("direct_cash", self.other))
        self.assertTrue(svc.option_enabled("direct_cash", str(self.other.pk)))

    def test_credit_follows_department_wallet_credit_switch(self):
        self.assertFalse(svc.department_allows("credit", self.dept))
        Department.objects.filter(pk=self.dept.pk).update(enable_wallet_credit=True)
        self.assertTrue(svc.department_allows("credit", self.dept))

    def test_missing_tables_fall_back_to_global_behaviour(self):
        self.masters(direct_cash_recharge_enabled=True)
        self.disable(self.dept, "direct_cash")
        with mock.patch.object(
            svc.WalletModeDepartmentSetting.objects, "filter", side_effect=ProgrammingError("no table")
        ):
            self.assertTrue(svc.option_enabled("direct_cash", self.dept))
        with mock.patch.object(
            svc.WalletModeEmailRecipients.objects, "filter", side_effect=ProgrammingError("no table")
        ):
            to, cc, source = svc.configured_recipients("project_grant", self.dept)
        self.assertEqual(source, "builtin")
        self.assertEqual(to, ["role:sric_office"])
        with mock.patch.object(
            svc.WalletPaymentModeConfig.objects, "filter", side_effect=ProgrammingError("no table")
        ):
            self.assertFalse(svc.direct_recharge_master_enabled())

    def test_user_flags_list_department_overrides(self):
        self.masters(direct_cash_recharge_enabled=True, peer_transfer_enabled=False)
        self.disable(self.dept, "direct_cash", "peer_transfer")
        data = self.api.get(SETTINGS).data
        self.assertTrue(data["direct_cash_recharge_enabled"])
        self.assertEqual(data["department_modes"], {str(self.dept.pk): {"direct_cash_recharge_enabled": False}})
        self.assertEqual(wallet_mode_flags(self.faculty)["department_modes"][str(self.dept.pk)], {"direct_cash_recharge_enabled": False})


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", WALLET_CREDIT_FACILITY_V2_ENABLED=True)
class EnforcementTests(Base):
    def _cash(self, dept):
        return self.api.post(
            SEND_OTP,
            {"amount": "1000.00", "department_id": dept.id, "recharge_mode": "direct_cash_deposit", "undertaking_accepted": True},
            format="json",
        )

    def test_cash_deposit_rejected_only_for_disabled_department(self):
        self.masters(direct_cash_recharge_enabled=True)
        self.disable(self.dept, "direct_cash")
        res = self._cash(self.dept)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "direct_cash_recharge_disabled")
        self.assertIn("Awaiting Competent Authority Approval", res.data["error"])
        self.assertEqual(self._cash(self.other).status_code, 200)

    def test_cash_draft_cannot_be_verified_after_department_disabled(self):
        self.masters(direct_cash_recharge_enabled=True)
        res = self._cash(self.dept)
        rid = res.data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        self.disable(self.dept, "direct_cash")
        res = self.api.post("/api/wallet/recharge-request/", {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "direct_cash_recharge_disabled")

    def test_project_grant_rejected_for_disabled_department(self):
        self.masters(project_grant_recharge_enabled=True)
        self.disable(self.dept, "project_grant")
        res = self.api.post(
            SEND_OTP,
            {"amount": "1000.00", "department_id": self.dept.id, "recharge_mode": "project_grant", "undertaking_accepted": True},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "project_grant_recharge_disabled")

    def test_gateway_order_rejected_for_disabled_department(self):
        self.masters(online_gateway_recharge_enabled=True)
        self.disable(self.dept, "online_gateway")
        for url, payload in (
            (GATEWAY_ORDER, {"purpose": "WALLET_RECHARGE", "amount": "500", "department_id": self.dept.id}),
            (LEGACY_GATEWAY_ORDER, {"amount": "500", "department_id": self.dept.id}),
        ):
            res = self.api.post(url, payload, format="json")
            self.assertEqual(res.status_code, 403, url)
            self.assertEqual(res.data["code"], "online_gateway_recharge_disabled")
        res = self.api.post(GATEWAY_ORDER, {"purpose": "WALLET_RECHARGE", "amount": "500", "department_id": self.other.id}, format="json")
        self.assertNotEqual(res.data.get("code"), "online_gateway_recharge_disabled")

    def test_peer_transfer_rejected_for_disabled_department(self):
        self.masters(peer_transfer_enabled=True)
        self.disable(self.dept, "peer_transfer")
        res = self.api.post(PEER_SEND_OTP, {"department_id": self.dept.id, "recipient_id": self.colleague.id, "amount": "10"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "peer_transfer_disabled")
        transfer = WalletPeerTransfer.objects.create(
            transaction_id="W2W-TEST-1", sender=self.faculty, recipient=self.colleague, initiated_by=self.faculty,
            department=self.dept, amount=Decimal("10"), otp_code="123456",
        )
        res = self.api.post(PEER_CONFIRM, {"transfer_id": transfer.id, "otp": "123456"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "peer_transfer_disabled")

    def test_credit_request_needs_department_credit_switch(self):
        policy = WalletCreditPolicy.get_solo()
        policy.enabled = True
        policy.min_request_amount = Decimal("100")
        policy.max_credit_amount = Decimal("5000")
        policy.save()
        with self.assertRaises(WalletCreditError) as ctx:
            create_and_submit_request(user=self.faculty, requested_amount=Decimal("500"), purpose="x", department_id=self.dept.id)
        self.assertEqual(ctx.exception.code, "CREDIT_NOT_ENABLED_FOR_DEPARTMENT")
        Department.objects.filter(pk=self.dept.pk).update(enable_wallet_credit=True)
        facility = create_and_submit_request(user=self.faculty, requested_amount=Decimal("500"), purpose="x", department_id=self.dept.id)
        self.assertEqual(facility.department_id, self.dept.id)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RecipientTests(Base):
    def setUp(self):
        super().setUp()
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = "sric.office@test.iitr.ac.in"
        s.bill_section_emails = "bills@test.iitr.ac.in"
        s.ar_sric_emails = "ar.sric@test.iitr.ac.in"
        s.dean_sric_emails = "dean.sric@test.iitr.ac.in"
        s.project_grant_cc_emails = "pg.cc@test.iitr.ac.in"
        s.cash_deposit_cc_emails = ""
        s.save()

    def _request(self, mode, dept=None):
        return WalletRechargeRequest.objects.create(
            user=self.faculty, wallet=self.wallet, department=dept or self.dept, amount=Decimal("500.00"),
            user_otp_verified=True, recharge_mode=mode, project_details="PRJ1",
        )

    def test_builtin_defaults_match_todays_recipients(self):
        req = self._request(WalletRechargeMode.PROJECT_GRANT)
        self.assertEqual(get_recharge_approver_emails(req), ["sric.office@test.iitr.ac.in"])
        self.assertEqual(
            get_recharge_cc_emails(WalletRechargeMode.PROJECT_GRANT, self.dept.pk),
            ["ar.sric@test.iitr.ac.in", "dean.sric@test.iitr.ac.in", "pg.cc@test.iitr.ac.in"],
        )
        cash = self._request(WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        self.assertEqual(get_recharge_approver_emails(cash), ["bills@test.iitr.ac.in"])

    def test_department_override_beats_default_row(self):
        WalletModeEmailRecipients.objects.create(option="project_grant", to_recipients=["default.to@test.iitr.ac.in"], cc_recipients=[])
        WalletModeEmailRecipients.objects.create(
            option="project_grant", department=self.dept, to_recipients=["chem.sric@test.iitr.ac.in"],
            cc_recipients=["chem.cc@test.iitr.ac.in", "role:dept_admin"],
        )
        dept_admin = User.objects.create_user(
            email="da.pm@test.iitr.ac.in", password="x12345678", name="DA", user_type=UserType.DEPT_ADMIN, department=self.dept, admin_approved=True
        )
        resolved = svc.resolve_recipients("project_grant", department=self.dept, requester=self.faculty)
        self.assertEqual(resolved.source, "department")
        self.assertEqual(resolved.to, ["chem.sric@test.iitr.ac.in"])
        self.assertEqual(resolved.cc, [self.faculty.email, "chem.cc@test.iitr.ac.in", dept_admin.email])
        other = svc.resolve_recipients("project_grant", department=self.other, requester=self.faculty)
        self.assertEqual((other.source, other.to), ("default", ["default.to@test.iitr.ac.in"]))

        send_sric_approval_email(self._request(WalletRechargeMode.PROJECT_GRANT))
        approval, copy = mail.outbox
        self.assertEqual(approval.to, ["chem.sric@test.iitr.ac.in"])
        self.assertEqual(copy.to, [self.faculty.email])
        self.assertEqual(copy.cc, ["chem.cc@test.iitr.ac.in", dept_admin.email])

    def test_requester_always_in_cc_and_deduplicated(self):
        WalletModeEmailRecipients.objects.create(
            option="online_gateway", to_recipients=["ops@test.iitr.ac.in"],
            cc_recipients=["OPS@test.iitr.ac.in", self.faculty.email, "x@test.iitr.ac.in", "x@test.iitr.ac.in"],
        )
        resolved = svc.resolve_recipients("online_gateway", department=self.dept, requester=self.faculty)
        self.assertEqual(resolved.to, ["ops@test.iitr.ac.in"])
        self.assertEqual(resolved.cc, [self.faculty.email, "x@test.iitr.ac.in"])

    def test_requester_never_gets_approval_links(self):
        WalletModeEmailRecipients.objects.create(
            option="project_grant", to_recipients=[self.faculty.email, "sric2@test.iitr.ac.in"], cc_recipients=[]
        )
        self.assertEqual(get_recharge_approver_emails(self._request(WalletRechargeMode.PROJECT_GRANT)), ["sric2@test.iitr.ac.in"])

    def test_normalize_validates_and_deduplicates(self):
        clean, errors = svc.normalize_recipients(["A@Test.iitr.ac.in", "a@test.iitr.ac.in; role:dept_oic", "bad@", "role:nope"])
        self.assertEqual(clean, ["a@test.iitr.ac.in", "role:dept_oic"])
        self.assertEqual(len(errors), 2)

    def test_peer_transfer_staff_copy_uses_configuration(self):
        transfer = WalletPeerTransfer(sender=self.faculty, recipient=self.colleague, department=self.dept, amount=Decimal("1"))
        incharge = User.objects.create_user(
            email="acc.pm@test.iitr.ac.in", password="x12345678", name="Acc", user_type=UserType.FINANCE, department=self.dept, admin_approved=True
        )
        self.assertIn(incharge.email, peer_transfer_staff_emails(transfer))
        WalletModeEmailRecipients.objects.create(
            option="peer_transfer", department=self.dept, to_recipients=["chem.office@test.iitr.ac.in"], cc_recipients=[]
        )
        self.assertEqual(peer_transfer_staff_emails(transfer), ["chem.office@test.iitr.ac.in"])

    def test_admin_api_saves_validates_and_resets(self):
        self.assertEqual(self.api.put(RECIPIENTS, {}, format="json").status_code, 403)
        res = self.admin_api.put(RECIPIENTS, {"option": "project_grant", "department_id": self.dept.id, "to": [], "cc": []}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.admin_api.put(
            RECIPIENTS, {"option": "project_grant", "department_id": self.dept.id, "to": ["role:wallet_owner"], "cc": []}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        res = self.admin_api.put(
            RECIPIENTS,
            {"option": "project_grant", "department_id": self.dept.id, "to": ["role:sric_office"], "cc": ["bad", "ok@test.iitr.ac.in"]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("cc", res.data["errors"])
        res = self.admin_api.put(
            RECIPIENTS,
            {"option": "project_grant", "department_id": self.dept.id, "to": ["role:sric_office"], "cc": ["Ok@test.iitr.ac.in", "role:sric_office"]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["cc"], ["ok@test.iitr.ac.in"])
        self.assertTrue(WalletPaymentModeAuditEvent.objects.filter(action="recipients_saved").exists())
        res = self.admin_api.delete(f"{RECIPIENTS}?option=project_grant&department_id={self.dept.id}")
        self.assertTrue(res.data["removed"])
        self.assertFalse(WalletModeEmailRecipients.objects.exists())


class AdminMatrixTests(Base):
    def test_overview_and_department_changes(self):
        self.assertEqual(self.api.get(OVERVIEW).status_code, 403)
        self.masters(direct_cash_recharge_enabled=True)
        res = self.admin_api.patch(
            DEPARTMENTS,
            {"changes": [
                {"department_id": self.dept.id, "option": "direct_cash", "state": "disabled"},
                {"department_id": self.dept.id, "option": "credit", "state": "enabled"},
            ]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["applied"], 2)
        self.dept.refresh_from_db()
        self.assertTrue(self.dept.enable_wallet_credit)
        data = self.admin_api.get(OVERVIEW).data
        row = next(d for d in data["departments"] if d["id"] == self.dept.id)
        self.assertEqual(row["states"]["direct_cash"], "disabled")
        self.assertFalse(row["effective"]["direct_cash"])
        self.assertEqual(row["states"]["credit"], "enabled")
        self.assertTrue(data["schema_ready"])
        bad = self.admin_api.patch(DEPARTMENTS, {"changes": [{"department_id": self.dept.id, "option": "x", "state": "disabled"}]}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_direct_recharge_master_switch_on_mode_settings(self):
        self.assertFalse(self.admin_api.get(MODES).data["direct_recharge_enabled"])
        res = self.admin_api.patch(f"{MODES}1/", {"direct_recharge_enabled": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["direct_recharge_enabled"])
        self.assertTrue(WalletPaymentModeConfig.objects.get(pk=1).direct_recharge_enabled)
        self.assertTrue(WalletPaymentModeAuditEvent.objects.filter(action="master_changed", target="direct_recharge").exists())


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DirectRechargeTests(Base):
    def setUp(self):
        super().setUp()
        self.designee_api = APIClient()
        self.designee_api.force_authenticate(self.designee)

    def enable(self):
        WalletPaymentModeConfig.objects.update_or_create(pk=1, defaults={"direct_recharge_enabled": True})

    def grant(self, **kw):
        now = timezone.now()
        defaults = {
            "user": self.designee, "valid_from": now - timedelta(hours=1), "valid_until": now + timedelta(days=1),
            "reason": "Year-end deposits", "granted_by": self.admin,
        }
        defaults.update(kw)
        return WalletDirectRechargeGrant.objects.create(**defaults)

    def payload(self, **kw):
        data = {
            "client_request_id": "req-0001-abcdef",
            "owner_id": self.faculty.id,
            "department_id": self.dept.id,
            "amount": "250.50",
            "mode": "bank_transfer",
            "reference_number": "UTR123",
            "transaction_date": timezone.localdate().isoformat(),
            "remarks": "Deposited at SRIC",
        }
        data.update(kw)
        return data

    def test_disabled_globally_blocks_everyone(self):
        res = self.admin_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "DIRECT_RECHARGE_DISABLED")
        self.assertIn("Awaiting Competent Authority Approval", res.data["error"])
        self.assertFalse(self.admin_api.get(ACCESS).data["allowed"])

    def test_department_disabled_blocks(self):
        self.enable()
        self.disable(self.dept, "direct_recharge")
        res = self.admin_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.data["code"], "DIRECT_RECHARGE_DEPARTMENT_DISABLED")

    def test_main_admin_recharge_ledger_audit_and_email(self):
        self.enable()
        preview = self.admin_api.post(PREVIEW, self.payload(), format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["balance_after"], "350.50")
        self.assertEqual(preview.data["owner"]["email"], self.faculty.email)
        self.assertEqual(preview.data["performed_as"], "main_admin")

        res = self.admin_api.post(RECHARGE, self.payload(), format="json", REMOTE_ADDR="10.1.2.3")
        self.assertEqual(res.status_code, 201, res.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, Decimal("350.50"))
        record = WalletDirectRecharge.objects.get()
        self.assertEqual((record.balance_before, record.balance_after), (Decimal("100.00"), Decimal("350.50")))
        self.assertTrue(record.reference.startswith("DWR-"))
        self.assertEqual(record.ip_address, "10.1.2.3")
        self.assertIsNone(record.grant_id)
        txn = SubWalletTransaction.objects.get(pk=record.sub_wallet_transaction_id)
        self.assertEqual((txn.transaction_type, txn.amount, txn.sub_wallet_id), ("credit", Decimal("250.50"), self.sub.id))
        self.assertIn(record.reference, txn.description)
        self.assertEqual(SubWalletTransaction.objects.filter(sub_wallet=self.sub).count(), 1)
        event = WalletPaymentModeAuditEvent.objects.get(action="direct_recharge_performed")
        self.assertEqual(event.actor_id, self.admin.id)
        self.assertEqual(event.ip_address, "10.1.2.3")

    def test_email_goes_to_owner_with_performer_and_configured_cc(self):
        self.enable()
        WalletModeEmailRecipients.objects.create(option="direct_recharge", to_recipients=[], cc_recipients=["accounts@test.iitr.ac.in"])
        with self.captureOnCommitCallbacks(execute=True):
            res = self.admin_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 201)
        message = mail.outbox[-1]
        self.assertEqual(message.to, [self.faculty.email])
        self.assertEqual(message.cc, [self.admin.email, "accounts@test.iitr.ac.in"])
        record = WalletDirectRecharge.objects.get()
        self.assertEqual(record.email_to, [self.faculty.email])

    def test_idempotent_by_client_request_id(self):
        self.enable()
        first = self.admin_api.post(RECHARGE, self.payload(), format="json")
        second = self.admin_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(WalletDirectRecharge.objects.count(), 1)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, Decimal("350.50"))
        clash = self.admin_api.post(RECHARGE, self.payload(amount="999"), format="json")
        self.assertEqual(clash.status_code, 409)

    def test_designated_person_with_valid_grant(self):
        self.enable()
        grant = self.grant(department=self.dept, max_amount_per_transaction=Decimal("1000"))
        access = self.designee_api.get(ACCESS).data
        self.assertTrue(access["allowed"])
        self.assertEqual([d["id"] for d in access["departments"]], [self.dept.id])
        res = self.designee_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["grant_id"], grant.id)
        self.assertEqual(res.data["performed_as"], "designated_person")
        history = self.designee_api.get(HISTORY).data
        self.assertEqual(history["count"], 1)

    def test_expired_revoked_or_missing_grant_is_rejected(self):
        self.enable()
        res = self.designee_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "NOT_AUTHORISED")
        self.grant(valid_from=timezone.now() - timedelta(days=3), valid_until=timezone.now() - timedelta(days=1))
        self.grant(revoked_at=timezone.now())
        res = self.designee_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "NOT_AUTHORISED")
        self.assertIn("expired", res.data["error"])
        self.assertFalse(self.designee_api.get(ACCESS).data["allowed"])

    def test_grant_scope_and_cap(self):
        self.enable()
        self.grant(department=self.other)
        res = self.designee_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.data["code"], "NOT_AUTHORISED")
        self.grant(max_amount_per_transaction=Decimal("100"))
        res = self.designee_api.post(RECHARGE, self.payload(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "OVER_GRANT_LIMIT")
        self.assertEqual(res.data["max_amount_per_transaction"], "100.00")
        res = self.designee_api.post(RECHARGE, self.payload(amount="100", client_request_id="req-0002-abcdef"), format="json")
        self.assertEqual(res.status_code, 201, res.data)

    def test_validation(self):
        self.enable()
        for bad, code in (
            ({"remarks": ""}, "REMARKS_REQUIRED"),
            ({"reference_number": ""}, "REFERENCE_REQUIRED"),
            ({"amount": "-5"}, "INVALID_AMOUNT"),
            ({"mode": "crypto"}, "INVALID_MODE"),
            ({"transaction_date": (timezone.localdate() + timedelta(days=2)).isoformat()}, "INVALID_DATE"),
            ({"client_request_id": "x"}, "CLIENT_REQUEST_ID_REQUIRED"),
        ):
            res = self.admin_api.post(RECHARGE, self.payload(**bad), format="json")
            self.assertEqual(res.status_code, 400, bad)
            self.assertEqual(res.data["code"], code)
        res = self.admin_api.post(RECHARGE, self.payload(mode="cash", reference_number=""), format="json")
        self.assertEqual(res.status_code, 201)

    def test_grant_admin_api_create_list_revoke(self):
        self.assertEqual(self.designee_api.get(GRANTS).status_code, 403)
        until = (timezone.localdate() + timedelta(days=5)).isoformat()
        res = self.admin_api.post(GRANTS, {"user_id": self.designee.id, "valid_until": until, "reason": ""}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.admin_api.post(
            GRANTS,
            {"user_id": self.designee.id, "valid_until": until, "reason": "Cover", "max_amount_per_transaction": "5000",
             "department_id": self.dept.id},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["status"], "active")
        gid = res.data["id"]
        self.assertEqual(self.admin_api.get(GRANTS).data["grants"][0]["id"], gid)
        res = self.admin_api.post(f"{GRANTS}{gid}/revoke/", {"reason": "Done"}, format="json")
        self.assertEqual(res.data["status"], "revoked")
        self.enable()
        self.assertEqual(self.designee_api.post(RECHARGE, self.payload(), format="json").status_code, 403)
