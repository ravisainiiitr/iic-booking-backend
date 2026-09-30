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
LOGIN_URL = "/api/auth/login/"
LOGIN_OTP_URL = "/api/auth/login/request-otp/"
FORGOT_OTP_URL = "/api/auth/forgot-password/request-otp/"
FORGOT_SET_URL = "/api/auth/forgot-password/verify-otp-and-set-password/"

NEW_PASSWORD = "Rk7!quartz-lattice"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountPasswordTests(TestCase):
    def setUp(self):
        cache.clear()

    def _channel_i_user(self, email, user_type=UserType.STUDENT, *, logged_in=True):
        user = User.objects.create_user(email=email, password=None, name="Channel User", user_type=user_type)
        user.email_verified = True
        user.admin_approved = True
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
        self.assertEqual(login.status_code, 401)
        self.assertEqual(len(mail.outbox), 0)

    def test_signed_in_channel_i_user_can_reset_forgotten_password(self):
        user = self._channel_i_user("stu.reset@test.iitr.ac.in")
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

    def test_external_user_forgot_password_unaffected(self):
        user = User.objects.create_user(
            email="ext.user@example.com", password="Ext-pass-4410", name="Ext", user_type=UserType.EXTERNAL
        )
        user.email_verified = True
        user.admin_approved = True
        user.save()
        resp = APIClient().post(FORGOT_OTP_URL, {"email": user.email}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
