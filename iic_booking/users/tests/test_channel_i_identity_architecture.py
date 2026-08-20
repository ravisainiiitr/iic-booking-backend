"""PostgreSQL tests for Channel-I identity, classification, mapping, HoD, lifecycle, extensions."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.identity.dates import add_calendar_months, add_calendar_years
from iic_booking.users.identity.extract import extract_channel_i_academic_facts, normalize_label
from iic_booking.users.identity.hod import assign_hod
from iic_booking.users.identity.lifecycle import (
    LifecycleError,
    approve_extension,
    expire_due_students,
    request_six_month_extension,
)
from iic_booking.users.identity.service import UserEligibilityService, UserIdentityService
from iic_booking.users.identity.sync import sync_channel_i_identity
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.channel_i_identity import (
    ChannelIDepartmentMapping,
    ChannelIIdentityHistory,
    DegreeClassificationKind,
    PortalUserClassification,
    StudentDegreeClassification,
    StudentValiditySource,
)
from iic_booking.users.models.wallet import SubWallet

User = get_user_model()

NESTED_STUDENT = {
    "userId": "9001",
    "username": "ug.student",
    "student": {
        "branch": {
            "name": "Mechanical",
            "degree": {"name": "B.Tech"},
            "department": {"name": "Department of Mechanical Engineering"},
        },
        "start_date": "2026-08-01",
        "end_date": None,
        "enrolmentNumber": "24117001",
    },
    "person": {"fullName": "UG Student"},
}


class ChannelIIdentityExtractTests(TestCase):
    def test_nested_degree_department_dates(self):
        facts = extract_channel_i_academic_facts(NESTED_STUDENT)
        self.assertEqual(facts["student_degree_name"], "B.Tech")
        self.assertEqual(facts["student_department_name"], "Department of Mechanical Engineering")
        self.assertEqual(facts["student_start_date"], date(2026, 8, 1))
        self.assertIsNone(facts["student_end_date"])
        self.assertEqual(facts["channel_i_user_id"], "9001")
        self.assertEqual(facts["channel_i_username"], "ug.student")

    def test_missing_values(self):
        facts = extract_channel_i_academic_facts({"userId": "1", "student": {}})
        self.assertEqual(facts["student_degree_name"], "")
        self.assertIsNone(facts["student_start_date"])


@override_settings(
    HOD_AFFILIATION_ENABLED=True,
    STUDENT_LIFECYCLE_ENABLED=True,
    DEPARTMENT_MAPPING_ENABLED=True,
    WALLET_CREDIT_FACILITY_V2_ENABLED=True,
)
class IdentityArchitectureTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="Mechanical Engineering",
            code="MECHID",
            department_type=DepartmentType.INTERNAL,
        )
        StudentDegreeClassification.objects.create(
            channel_i_degree_name="B.Tech",
            channel_i_degree_name_normalized=normalize_label("B.Tech"),
            classification=DegreeClassificationKind.UNDERGRADUATE,
            active=True,
        )
        StudentDegreeClassification.objects.create(
            channel_i_degree_name="M.Tech",
            channel_i_degree_name_normalized=normalize_label("M.Tech"),
            classification=DegreeClassificationKind.POSTGRADUATE,
            active=True,
        )
        ChannelIDepartmentMapping.objects.create(
            channel_i_department_name="Department of Mechanical Engineering",
            channel_i_department_name_normalized=normalize_label("Department of Mechanical Engineering"),
            internal_department=self.dept,
            active=True,
        )
        self.admin = User.objects.create_user(
            email="admin.ident@test.iitr.ac.in",
            password="pass12345",
            name="Admin",
            user_type=UserType.ADMIN,
            admin_approved=True,
        )
        self.hod = User.objects.create_user(
            email="hod.mech@test.iitr.ac.in",
            password="pass12345",
            name="HoD Mech",
            user_type=UserType.FACULTY,
            department=self.dept,
            admin_approved=True,
        )
        self.other_hod = User.objects.create_user(
            email="hod.ee@test.iitr.ac.in",
            password="pass12345",
            name="HoD EE",
            user_type=UserType.FACULTY,
            admin_approved=True,
        )
        self.ee = Department.objects.create(
            name="Electrical Engineering",
            code="EEID",
            department_type=DepartmentType.INTERNAL,
        )
        self.faculty = User.objects.create_user(
            email="fac.ident@test.iitr.ac.in",
            password="pass12345",
            name="Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            admin_approved=True,
        )
        Wallet.objects.create(user=self.hod)
        Wallet.objects.create(user=self.other_hod)
        Wallet.objects.create(user=self.faculty)
        SubWallet.objects.create(wallet=self.faculty.wallet, department=self.dept, balance=Decimal("1000.00"))
        self.ug = User.objects.create_user(
            email="ug.ident@test.iitr.ac.in",
            password="pass12345",
            name="UG",
            user_type=UserType.STUDENT,
            department=self.dept,
            degree_name="B.Tech",
            admin_approved=True,
        )
        self.pg = User.objects.create_user(
            email="pg.ident@test.iitr.ac.in",
            password="pass12345",
            name="PG",
            user_type=UserType.STUDENT,
            degree_name="M.Tech",
            admin_approved=True,
        )
        self.unknown = User.objects.create_user(
            email="unk.ident@test.iitr.ac.in",
            password="pass12345",
            name="Unknown",
            user_type=UserType.STUDENT,
            degree_name="Secret Diploma",
            admin_approved=True,
        )
        sync_channel_i_identity(self.ug, NESTED_STUDENT)
        sync_channel_i_identity(
            self.pg,
            {
                "userId": "2",
                "username": "pg",
                "student": {
                    "branch": {"degree": {"name": "M.Tech"}, "department": {"name": "Department of Mechanical Engineering"}},
                    "start_date": "2026-08-01",
                    "end_date": "2028-07-31",
                },
            },
        )
        sync_channel_i_identity(
            self.unknown,
            {
                "userId": "3",
                "username": "unk",
                "student": {"branch": {"degree": {"name": "Secret Diploma"}, "department": {"name": "Unknown Dept"}}},
            },
        )
        assign_hod(department=self.dept, user=self.hod, actor=self.admin)
        assign_hod(department=self.ee, user=self.other_hod, actor=self.admin)
        self.client = APIClient()

    def test_classification_undergraduate_pg_unknown(self):
        self.assertEqual(UserIdentityService.classify_user(self.ug), PortalUserClassification.UNDERGRADUATE_STUDENT)
        self.assertEqual(UserIdentityService.classify_user(self.pg), PortalUserClassification.OTHER_STUDENT)
        self.assertEqual(UserIdentityService.classify_user(self.unknown), PortalUserClassification.UNKNOWN)
        self.assertEqual(UserIdentityService.classify_user(self.hod), PortalUserClassification.HEAD_OF_DEPARTMENT)
        self.assertEqual(UserIdentityService.classify_user(self.faculty), PortalUserClassification.FACULTY)
        self.assertEqual(UserIdentityService.classify_user(self.admin), PortalUserClassification.STAFF)

    def test_profile_history_on_department_change(self):
        sync_channel_i_identity(
            self.ug,
            {
                **NESTED_STUDENT,
                "student": {
                    **NESTED_STUDENT["student"],
                    "branch": {
                        "degree": {"name": "B.Tech"},
                        "department": {"name": "Department of Production Engineering"},
                    },
                    "start_date": "2026-08-01",
                },
            },
        )
        hist = ChannelIIdentityHistory.objects.filter(
            profile__user=self.ug, field_name="student_department_name"
        )
        self.assertTrue(hist.exists())
        self.ug.refresh_from_db()
        # Portal department FK is not rewritten by Channel-I mapping.
        self.assertEqual(self.ug.department_id, self.dept.id)

    def test_unmapped_department_status(self):
        view = UserIdentityService.view(self.unknown)
        self.assertEqual(view.department_status, "UNMAPPED")

    def test_hod_join_enforcement(self):
        view = UserIdentityService.view(self.ug)
        self.assertEqual(view.classification, PortalUserClassification.UNDERGRADUATE_STUDENT)
        self.assertEqual(view.department_status, "MAPPED")
        ok, code, msg = UserEligibilityService.evaluate_hod_join(self.ug, self.hod)
        self.assertTrue(ok, msg=f"{code}: {msg}")
        ok, code, _ = UserEligibilityService.evaluate_hod_join(self.ug, self.other_hod)
        self.assertFalse(ok)
        self.assertEqual(code, "HOD_DEPARTMENT_MISMATCH")
        ok, code, _ = UserEligibilityService.evaluate_hod_join(self.pg, self.hod)
        self.assertFalse(ok)
        self.assertEqual(code, "HOD_NOT_AVAILABLE_FOR_USER_TYPE")
        ok, code, _ = UserEligibilityService.evaluate_hod_join(self.unknown, self.hod)
        self.assertFalse(ok)
        self.assertIn(code, {"USER_TYPE_UNRESOLVED", "HOD_NOT_AVAILABLE_FOR_USER_TYPE", "STUDENT_DEPARTMENT_UNRESOLVED"})

        self.client.force_authenticate(user=self.ug)
        res = self.client.post(
            "/api/wallet/join-request/",
            {"faculty_email": self.other_hod.email, "message": "wrong hod"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data.get("code"), "HOD_DEPARTMENT_MISMATCH")

        res = self.client.post(
            "/api/wallet/join-request/",
            {"faculty_email": self.faculty.email, "message": "normal faculty"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_validity_five_years_and_channel_i_end(self):
        v_ug = UserIdentityService.view(self.ug).validity
        self.assertEqual(v_ug.validity_source, StudentValiditySource.START_DATE_PLUS_5_YEARS)
        self.assertEqual(v_ug.derived_end_date, date(2031, 8, 1))
        v_pg = UserIdentityService.view(self.pg).validity
        self.assertEqual(v_pg.validity_source, StudentValiditySource.CHANNEL_I_END_DATE)
        self.assertEqual(v_pg.channel_i_end_date, date(2028, 7, 31))
        v_unk = UserIdentityService.view(self.unknown).validity
        self.assertTrue(v_unk.unresolved)

    def test_expire_and_extension(self):
        profile = UserIdentityService.get_profile(self.ug)
        profile.derived_end_date = date(2020, 1, 1)
        profile.student_start_date = date(2015, 1, 1)
        profile.save()
        n = expire_due_students()
        self.assertGreaterEqual(n, 1)
        self.ug.refresh_from_db()
        self.assertFalse(self.ug.is_active)
        self.assertTrue(self.ug.force_inactive)

        # Channel-I end date students cannot be extended (even by wallet owner)
        from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

        WalletJoinRequest.objects.create(
            student=self.pg,
            faculty=self.faculty,
            wallet=self.faculty.wallet,
            status=WalletJoinRequestStatus.APPROVED,
        )
        with self.assertRaises(LifecycleError) as ctx:
            request_six_month_extension(student=self.pg, faculty=self.faculty, reason="try")
        self.assertEqual(ctx.exception.code, "CHANNEL_I_END_DATE_AUTHORITATIVE")

        # Restore UG for extension tests
        self.ug.force_inactive = False
        self.ug.admin_approved = True
        self.ug.is_active = True
        self.ug.save()
        profile = UserIdentityService.get_profile(self.ug)
        profile.student_end_date = None
        profile.student_start_date = date(2026, 8, 1)
        profile.derived_end_date = add_calendar_years(date(2026, 8, 1), 5)
        profile.save()
        with self.assertRaises(LifecycleError):
            request_six_month_extension(student=self.ug, faculty=self.faculty, reason="not owner")

        WalletJoinRequest.objects.create(
            student=self.ug,
            faculty=self.faculty,
            wallet=self.faculty.wallet,
            status=WalletJoinRequestStatus.APPROVED,
        )
        ext = request_six_month_extension(student=self.ug, faculty=self.faculty, reason="project")
        self.assertEqual(ext.extension_months, 6)
        self.assertEqual(ext.requested_expiry, add_calendar_months(date(2031, 8, 1), 6))
        ext = approve_extension(extension=ext, admin=self.admin)
        self.assertEqual(ext.status, "APPROVED")
        profile.refresh_from_db()
        self.assertEqual(profile.derived_end_date, date(2032, 2, 1))
        self.assertEqual(profile.validity_source, StudentValiditySource.ADMIN_EXTENSION)

        # Channel-I end later overrides local extension
        sync_channel_i_identity(
            self.ug,
            {
                "userId": "9001",
                "username": "ug.student",
                "student": {
                    "branch": {
                        "degree": {"name": "B.Tech"},
                        "department": {"name": "Department of Mechanical Engineering"},
                    },
                    "start_date": "2026-08-01",
                    "end_date": "2030-06-30",
                },
            },
        )
        v = UserIdentityService.view(self.ug).validity
        self.assertEqual(v.validity_source, StudentValiditySource.CHANNEL_I_END_DATE)
        self.assertEqual(v.effective_end_date, date(2030, 6, 30))

    def test_calendar_months_not_180_days(self):
        self.assertEqual(add_calendar_months(date(2026, 8, 31), 6), date(2027, 2, 28))

    def test_wallet_credit_students_denied(self):
        from iic_booking.users.models.wallet_credit_facility import WalletCreditPolicy

        policy = WalletCreditPolicy.get_solo()
        policy.enabled = True
        policy.save()
        ok, code, _ = UserEligibilityService.can_request_wallet_credit(self.ug)
        self.assertFalse(ok)
        self.assertEqual(code, "CREDIT_NOT_ALLOWED_FOR_USER_TYPE")
        ok, code, _ = UserEligibilityService.can_request_wallet_credit(self.faculty)
        self.assertTrue(ok)

    def test_idor_degree_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/admin/identity/degrees/")
        self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/admin/identity/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("departments_unmapped", res.data)
