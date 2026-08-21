"""Channel-I identity extraction and mapping rules. PostgreSQL via Django test settings."""

from decimal import Decimal

import pytest
from django.test import TestCase

from iic_booking.users.legacy_ledger.channel_i_identity import (
    classify_legacy_identity,
    decide_employee_id_on_login,
    extract_channel_i_identity,
    is_wallet_migration_eligible,
    looks_like_iic_operator_code,
)
from iic_booking.users.oauth_redact import REDACTED, redact_oauth_payload, redact_oauth_text, userinfo_key_summary
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.mapping import classify_old_user
from iic_booking.users.legacy_ledger.reconcile import run_full_reconciliation
from iic_booking.users.models.portal_migration import LegacyWalletMappingStatus
from iic_booking.users.tests.factories import UserFactory


STUDENT_USERINFO = {
    "userId": "4242",
    "username": "23117122",
    "person": {"fullName": "Example Student"},
    "student": {"enrolmentNumber": "23117122", "branch name": "ECE"},
    "contactInformation": {"instituteWebmailAddress": "ex.student@iitr.ac.in"},
}

FACULTY_WITH_EMPLOYEE_CLAIM = {
    "userId": "99",
    "username": "some.login",
    "person": {"fullName": "Example Faculty"},
    "facultyMember": {"employeeId": "100426", "department name": "IIC"},
    "contactInformation": {"instituteWebmailAddress": "fac@iitr.ac.in"},
}

OPERATOR_USERNAME = {
    "userId": "7",
    "username": "IICNMR",
    "person": {"fullName": "NMR Operator"},
    "roles": [{"role": "Maintainer"}],
    "contactInformation": {"instituteWebmailAddress": "nmr@iitr.ac.in"},
}


class TestChannelIExtraction:
    def test_student_enrolment_is_candidate_not_verified(self):
        c = extract_channel_i_identity(STUDENT_USERINFO)
        assert c.provider_subject == "4242"
        assert c.channel_i_username == "23117122"
        assert c.student_enrolment_number == "23117122"
        assert c.candidate_employee_id == "23117122"
        assert c.candidate_employee_id_source == "student.enrolmentNumber"
        assert c.verified is False

    def test_faculty_employee_claim_preferred_over_username(self):
        c = extract_channel_i_identity(FACULTY_WITH_EMPLOYEE_CLAIM)
        assert c.faculty_employee_id_claim == "100426"
        assert c.candidate_employee_id == "100426"
        assert c.channel_i_username == "some.login"
        assert c.username_equals_candidate is False
        assert c.verified is False

    def test_username_is_not_employee_id_candidate(self):
        c = extract_channel_i_identity(OPERATOR_USERNAME)
        assert c.username_is_operator_code is True
        assert looks_like_iic_operator_code("IICNMR") is True
        assert c.candidate_employee_id == ""
        decision = decide_employee_id_on_login(
            existing_emp_id="",
            claims=c,
            other_user_has_candidate=False,
        )
        assert decision.action == "skip"
        assert decision.status == "UNVERIFIED"

    def test_corrected_impl_does_not_copy_username_to_emp_id(self):
        c = extract_channel_i_identity(OPERATOR_USERNAME)
        assert c.channel_i_username == "IICNMR"
        assert c.candidate_employee_id_source != "username_unverified"

    def test_no_fuzzy_fields_in_extractor(self):
        c = extract_channel_i_identity(
            {
                "userId": "1",
                "username": "",
                "person": {"fullName": "Only Name"},
                "contactInformation": {"instituteWebmailAddress": "a@iitr.ac.in"},
            }
        )
        assert c.candidate_employee_id == ""
        assert c.email == "a@iitr.ac.in"


class TestBackfillRules:
    def test_empty_emp_id_not_populated_when_unverified(self):
        c = extract_channel_i_identity(STUDENT_USERINFO)
        d = decide_employee_id_on_login(
            existing_emp_id="",
            claims=c,
            other_user_has_candidate=False,
        )
        assert d.action == "skip"
        assert d.status == "UNVERIFIED"

    def test_does_not_overwrite_existing_emp_id(self):
        c = extract_channel_i_identity(STUDENT_USERINFO)
        d = decide_employee_id_on_login(
            existing_emp_id="999999",
            claims=c,
            other_user_has_candidate=False,
        )
        assert d.action == "unchanged"
        assert d.employee_id == "999999"

    def test_verified_faculty_claim_sets_empty_emp_id(self):
        c = extract_channel_i_identity(FACULTY_WITH_EMPLOYEE_CLAIM)
        d = decide_employee_id_on_login(
            existing_emp_id="",
            claims=c,
            other_user_has_candidate=False,
            operator_confirmed_claim="facultyMember.employeeId",
        )
        assert d.action == "set_verified"
        assert d.employee_id == "100426"
        assert d.status == "CHANNEL_I_VERIFIED"

    def test_duplicate_verified_candidate_skipped(self):
        c = extract_channel_i_identity(FACULTY_WITH_EMPLOYEE_CLAIM)
        d = decide_employee_id_on_login(
            existing_emp_id="",
            claims=c,
            other_user_has_candidate=True,
            operator_confirmed_claim="facultyMember.employeeId",
        )
        assert d.action == "skip"
        assert d.status == "CONFLICT"

    def test_verified_conflict_does_not_overwrite(self):
        c = extract_channel_i_identity(FACULTY_WITH_EMPLOYEE_CLAIM)
        d = decide_employee_id_on_login(
            existing_emp_id="111111",
            claims=c,
            other_user_has_candidate=False,
            operator_confirmed_claim="facultyMember.employeeId",
        )
        assert d.action == "conflict"
        assert d.employee_id == "111111"

    def test_provider_subject_extracted(self):
        c = extract_channel_i_identity(STUDENT_USERINFO)
        assert c.provider_subject == "4242"


class TestLegacyClassification:
    def test_missing(self):
        assert classify_legacy_identity("", duplicate_ids=set(), has_wallet=True) == "NO_EMP_ID"

    def test_duplicate(self):
        assert classify_legacy_identity("100", duplicate_ids={"100"}, has_wallet=True) == "DUPLICATE_EMP_ID"

    def test_authoritative_unique(self):
        assert (
            classify_legacy_identity("100426", duplicate_ids=set(), has_wallet=True)
            == "AUTHORITATIVE_EMP_ID"
        )


@pytest.mark.django_db
class TestNoFuzzyWalletMapping(TestCase):
    def test_email_match_is_not_used(self):
        UserFactory(email="same@iitr.ac.in", emp_id="AAAA", name="New")
        row = classify_old_user(
            {"id": 1, "emp_id": "BBBB", "name": "New", "email": "same@iitr.ac.in"},
            set(),
        )
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.CHANNEL_I_NOT_FOUND)
        self.assertIsNone(row.new_user_id)

    def test_name_match_is_not_used(self):
        UserFactory(email="x@iitr.ac.in", emp_id="AAAA", name="Ravi")
        row = classify_old_user({"id": 1, "emp_id": "BBBB", "name": "Ravi", "email": "y@x"}, set())
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.CHANNEL_I_NOT_FOUND)

    def test_manual_missing_emp_id_exception(self):
        row = classify_old_user({"id": 88, "emp_id": "", "name": "A", "email": "a@x"}, set())
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.MISSING_EMPLOYEE_ID)
        self.assertIn("WALLET_MAPPING_EXCEPTION", row.exception_reason)


@pytest.mark.django_db
class TestWalletMismatchDetection(TestCase):
    def test_poison_pair_nets_zero_but_is_not_dropped(self):
        from datetime import datetime, timezone as dt_timezone

        from iic_booking.users.legacy_ledger.sync import run_ledger_sync, run_mapping

        user = UserFactory(email="p@iitr.ac.in", emp_id="321700", name="Poison", admin_approved=True)
        reader = FakeOldMySQLReader(
            [{"id": 3217, "emp_id": "321700", "name": "P", "email": "old@x"}],
            {3217: {"id": 9, "user_id": 3217, "balance": Decimal("0.00")}},
            [
                {
                    "id": 34098,
                    "user_id": 3217,
                    "amount": "8377862463",
                    "balance": "8377862463",
                    "transaction_type": 1,
                    "create_date": datetime(2024, 1, 9, tzinfo=dt_timezone.utc),
                    "description": "IMPS",
                },
                {
                    "id": 34256,
                    "user_id": 3217,
                    "amount": "8377862463",
                    "balance": "0",
                    "transaction_type": 2,
                    "create_date": datetime(2024, 1, 11, tzinfo=dt_timezone.utc),
                    "description": "debited due to wrong amount.",
                },
            ],
        )
        run_mapping(reader, batch="poison", dry_run=False, require_verified_identity=False)
        result = run_ledger_sync(reader, batch="poison", dry_run=False)
        self.assertTrue(result["ok"])
        recon = run_full_reconciliation()
        self.assertEqual(recon["counts"]["FAIL"], 0)
        from iic_booking.users.models.portal_migration import LegacyWalletLedgerEntry

        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 2)
        _ = user


class TestWalletEligibilityGate:
    def test_unverified_not_eligible(self):
        ok, reason = is_wallet_migration_eligible(
            employee_id="100426",
            production_user_count=1,
            identity_source="LEGACY_UNVERIFIED",
        )
        assert ok is False
        assert "not verified" in reason

    def test_verified_unique_active_eligible(self):
        ok, reason = is_wallet_migration_eligible(
            employee_id="100426",
            production_user_count=1,
            identity_source="CHANNEL_I_VERIFIED",
        )
        assert ok is True
        assert reason == "MIGRATION_ELIGIBLE"

    def test_iic_code_not_eligible(self):
        ok, _ = is_wallet_migration_eligible(
            employee_id="IICNMR",
            production_user_count=1,
            identity_source="CHANNEL_I_VERIFIED",
        )
        assert ok is False

    def test_duplicate_production_not_eligible(self):
        ok, _ = is_wallet_migration_eligible(
            employee_id="100426",
            production_user_count=2,
            identity_source="CHANNEL_I_VERIFIED",
        )
        assert ok is False


@pytest.mark.django_db
class TestVerifiedIdentityRequiredForWalletMap(TestCase):
    def test_exact_emp_id_without_verification_is_exception(self):
        UserFactory(email="fac@iitr.ac.in", emp_id="23EUCCE954", name="Faculty")
        row = classify_old_user(
            {"id": 9, "emp_id": "23EUCCE954", "name": "Old Name", "email": "old@x"},
            set(),
        )
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.EXCEPTION)
        self.assertIsNone(row.new_user_id)

    def test_exact_emp_id_with_verification_is_valid(self):
        user = UserFactory(email="fac2@iitr.ac.in", emp_id="23EUCCE955", name="Faculty", admin_approved=True)
        row = classify_old_user(
            {"id": 9, "emp_id": "23EUCCE955", "name": "Old Name", "email": "old@x"},
            set(),
            identity_source="CHANNEL_I_VERIFIED",
        )
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.VALID)
        self.assertEqual(row.new_user_id, user.pk)


class TestOAuthRedaction:
    def test_redacts_token_fields(self):
        payload = {
            "access_token": "super-secret",
            "refresh_token": "also-secret",
            "id_token": "jwt-secret",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        out = redact_oauth_payload(payload)
        assert out["access_token"] == REDACTED
        assert out["refresh_token"] == REDACTED
        assert out["id_token"] == REDACTED
        assert out["expires_in"] == 3600
        blob = str(out)
        assert "super-secret" not in blob
        assert "also-secret" not in blob
        assert "jwt-secret" not in blob

    def test_redacts_authorization_code_and_client_secret(self):
        out = redact_oauth_payload(
            {"code": "authz-code-xyz", "client_secret": "shh", "state": "abc"}
        )
        assert out["code"] == REDACTED
        assert out["client_secret"] == REDACTED
        assert out["state"] == "abc"

    def test_redacts_token_error_bodies(self):
        text = '{"access_token":"leak","error":"invalid"}'
        assert redact_oauth_text(text) == REDACTED

    def test_userinfo_summary_has_no_values(self):
        summary = userinfo_key_summary(STUDENT_USERINFO)
        blob = str(summary)
        assert "ex.student@iitr.ac.in" not in blob
        assert "Example Student" not in blob
        assert "userId" in summary["top_level_keys"]
        assert "username" in summary["top_level_keys"]

