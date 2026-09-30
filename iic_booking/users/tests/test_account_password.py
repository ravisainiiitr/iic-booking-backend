"""Dual login for Channel i users: set/change password from profile; first sign-in must be via Channel i."""

from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import UserType

User = get_user_model()

PASSWORD_URL = "/api/auth/password/"
EMAIL_LOGIN_URL = "/api/auth/email-login/"
LOGIN_URL = "/api/auth/login/"
LOGIN_OTP_URL = "/api/auth/login/request-otp/"
FORGOT_OTP_URL = "/api/auth/forgot-password/request-otp/"
FORGOT_SET_URL = "/api/auth/forgot-password/verify-otp-and-set-password/"

NEW_PASSWORD = "Rk7!quartz-lattice"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountPasswordTests(TestCase):
    def setUp(self):
        cache.clear()

    def _channel_i_user(self, email, user_type=UserType.STUDENT, *, logged_in=True, email_login=None):
        user = User.objects.create_user(email=email, password=None, name="Channel User", user_type=user_type)
        user.email_verified = True
        user.admin_approved = True
        user.email_login_enabled = email_login
        if logged_in:
            user.last_login = timezone.now()
        user.save()
        return user

    def test_channel_i_user_sets_password_then_logs_in_with_email(self):
        user = self._channel_i_user("stu.dual@test.iitr.ac.in")
        api = APIClient()
        api.force_authenticate(user)

        status_resp = api.get(PASSWORD_URL)
        self.assertEqual(status_resp.status_code, 200)
        self.assertFalse(status_resp.data["has_password"])
        self.assertTrue(status_resp.data["email_login_toggle"])
        self.assertFalse(status_resp.data["email_login_enabled"])

        blocked = api.post(
            PASSWORD_URL,
            {"new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)

        on = api.post(EMAIL_LOGIN_URL, {"enabled": True}, format="json")
        self.assertEqual(on.status_code, 200, on.data)
        self.assertTrue(on.data["email_login_enabled"])
        mail.outbox.clear()

        resp = api.post(
            PASSWORD_URL,
            {"new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(len(mail.outbox), 1)

        login = APIClient().post(LOGIN_URL, {"email": user.email, "password": NEW_PASSWORD}, format="json")
        self.assertEqual(login.status_code, 200, login.data)
        self.assertIn("token", login.data)

    def test_change_requires_current_password(self):
        user = self._channel_i_user("oic.dual@test.iitr.ac.in", UserType.MANAGER)
        user.set_password("Old-pass-9921")
        user.save()
        api = APIClient()
        api.force_authenticate(user)

        wrong = api.post(
            PASSWORD_URL,
            {"current_password": "nope", "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(wrong.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Old-pass-9921"))

        ok = api.post(
            PASSWORD_URL,
            {"current_password": "Old-pass-9921", "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_mismatched_confirmation_rejected(self):
        user = self._channel_i_user("lab.dual@test.iitr.ac.in", UserType.OPERATOR)
        api = APIClient()
        api.force_authenticate(user)
        resp = api.post(
            PASSWORD_URL,
            {"new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD + "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())

    def test_password_endpoint_requires_authentication(self):
        self.assertIn(APIClient().get(PASSWORD_URL).status_code, (401, 403))

    def test_never_signed_in_channel_i_user_cannot_bypass_channel_i(self):
        user = self._channel_i_user("fac.new@test.iitr.ac.in", UserType.FACULTY, logged_in=False)
        client = APIClient()

        self.assertEqual(client.post(LOGIN_OTP_URL, {"email": user.email}, format="json").status_code, 403)
        self.assertEqual(client.post(FORGOT_OTP_URL, {"email": user.email}, format="json").status_code, 403)
        login = client.post(LOGIN_URL, {"email": user.email, "password": "anything-123"}, format="json")
        self.assertEqual(login.status_code, 403)
        self.assertEqual(login.data["code"], "email_login_disabled")
        self.assertEqual(len(mail.outbox), 0)

    def test_never_signed_in_with_email_login_on_still_needs_channel_i_first(self):
        user = self._channel_i_user("fac.first@test.iitr.ac.in", UserType.FACULTY, logged_in=False, email_login=True)
        client = APIClient()
        self.assertEqual(client.post(FORGOT_OTP_URL, {"email": user.email}, format="json").status_code, 403)
        login = client.post(LOGIN_URL, {"email": user.email, "password": "anything-123"}, format="json")
        self.assertEqual(login.status_code, 401)
        self.assertEqual(len(mail.outbox), 0)

    def test_signed_in_channel_i_user_can_reset_forgotten_password(self):
        user = self._channel_i_user("stu.reset@test.iitr.ac.in", email_login=True)
        client = APIClient()

        sent = client.post(FORGOT_OTP_URL, {"email": user.email}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        otp = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

        reset = client.post(
            FORGOT_SET_URL,
            {
                "email": user.email,
                "otp": otp,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(reset.status_code, 200, reset.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_blank_or_unverifiable_password_counts_as_not_set(self):
        for stored in ("", "legacy$not-a-django-hash"):
            with self.subTest(stored=stored):
                user = self._channel_i_user(
                    f"fac.blank{len(stored)}@test.iitr.ac.in", UserType.FACULTY, email_login=True
                )
                User.objects.filter(pk=user.pk).update(password=stored)
                user.refresh_from_db()
                self.assertTrue(user.has_usable_password())
                api = APIClient()
                api.force_authenticate(user)

                self.assertFalse(api.get(PASSWORD_URL).data["has_password"])
                resp = api.post(
                    PASSWORD_URL,
                    {"new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
                    format="json",
                )
                self.assertEqual(resp.status_code, 200, resp.data)
                user.refresh_from_db()
                self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_forgot_password_matches_email_case_insensitively(self):
        user = self._channel_i_user("Mixed.Case@test.iitr.ac.in", email_login=True)
        client = APIClient()
        sent = client.post(FORGOT_OTP_URL, {"email": "mixed.case@test.iitr.ac.in"}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        otp = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        reset = client.post(
            FORGOT_SET_URL,
            {
                "email": "mixed.case@test.iitr.ac.in",
                "otp": otp,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(reset.status_code, 200, reset.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_external_user_forgot_password_unaffected(self):
        user = User.objects.create_user(
            email="ext.user@example.com", password="Ext-pass-4410", name="Ext", user_type=UserType.EXTERNAL
        )
        user.email_verified = True
        user.admin_approved = True
        user.save()
        resp = APIClient().post(FORGOT_OTP_URL, {"email": user.email}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailLoginToggleTests(TestCase):
    def setUp(self):
        cache.clear()

    def _user(self, email, user_type, *, alias="", password="Known-pass-7731", email_login=None):
        user = User.objects.create_user(email=email, password=password, name="U", user_type=user_type)
        user.email_verified = True
        user.admin_approved = True
        user.user_type_alias = alias or None
        user.email_login_enabled = email_login
        user.last_login = timezone.now()
        user.save()
        return user

    def test_defaults_by_user_type(self):
        cases = [
            (UserType.STUDENT, "", True, False),
            (UserType.INDIVIDUAL_STUDENT, "", True, False),
            (UserType.FACULTY, "", True, False),
            (UserType.MANAGER, "", True, True),
            (UserType.OPERATOR, "", True, True),
            (UserType.STUDENT, "IITR Post Doctoral Fellows", False, True),
            (UserType.EXTERNAL, "", False, True),
            (UserType.ADMIN, "", False, True),
            (UserType.DEPT_ADMIN, "", False, True),
        ]
        for i, (user_type, alias, toggle, allowed) in enumerate(cases):
            with self.subTest(user_type=user_type, alias=alias):
                user = self._user(f"u{i}@test.iitr.ac.in", user_type, alias=alias)
                self.assertEqual(user.has_email_login_toggle(), toggle)
                self.assertEqual(user.is_email_login_allowed(), allowed)

    def test_seeded_test_accounts_keep_email_login(self):
        user = self._user("test.student@iic-booking.test", UserType.STUDENT)
        self.assertFalse(user.has_email_login_toggle())
        self.assertTrue(user.is_email_login_allowed())

    def test_toggle_controls_password_and_otp_login(self):
        user = self._user("fac.toggle@test.iitr.ac.in", UserType.FACULTY)
        client = APIClient()
        creds = {"email": user.email, "password": "Known-pass-7731"}

        off = client.post(LOGIN_URL, creds, format="json")
        self.assertEqual(off.status_code, 403)
        self.assertEqual(off.data["code"], "email_login_disabled")
        self.assertEqual(client.post(LOGIN_OTP_URL, {"email": user.email}, format="json").status_code, 403)

        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post(EMAIL_LOGIN_URL, {"enabled": True}, format="json").status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("turned on", mail.outbox[0].subject)
        self.assertEqual(client.post(LOGIN_URL, creds, format="json").status_code, 200)

        user.refresh_from_db()
        api.force_authenticate(user)
        self.assertEqual(api.post(EMAIL_LOGIN_URL, {"enabled": False}, format="json").status_code, 200)
        self.assertEqual(APIClient().post(LOGIN_URL, creds, format="json").status_code, 403)

    def test_oic_can_turn_email_login_off(self):
        user = self._user("oic.off@test.iitr.ac.in", UserType.MANAGER)
        creds = {"email": user.email, "password": "Known-pass-7731"}
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post(EMAIL_LOGIN_URL, {"enabled": False}, format="json").status_code, 200)
        self.assertEqual(APIClient().post(LOGIN_URL, creds, format="json").status_code, 403)

    def test_toggle_rejected_for_users_without_it(self):
        user = self._user("ext.toggle@example.com", UserType.EXTERNAL)
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post(EMAIL_LOGIN_URL, {"enabled": False}, format="json").status_code, 400)
        self.assertTrue(api.get(PASSWORD_URL).data["email_login_enabled"])
        self.assertFalse(api.get(PASSWORD_URL).data["email_login_toggle"])

    def test_toggle_requires_boolean(self):
        user = self._user("stu.bool@test.iitr.ac.in", UserType.STUDENT)
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post(EMAIL_LOGIN_URL, {"enabled": "yes"}, format="json").status_code, 400)
