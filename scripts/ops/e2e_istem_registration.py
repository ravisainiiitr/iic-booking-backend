"""
End-to-end check on production of registering with an existing I-STEM account (against I-STEM staging):
  0. creates one fictitious user on I-STEM staging (non-deliverable example.com email), unless E2E_ISTEM_EMAIL names
     one created earlier;
  1. the option is offered only with the registration test key;
  2. look-up by email finds the I-STEM account and sends an OTP (captured here, not emailed);
  3. a wrong OTP is refused; the right one returns the I-STEM profile and a link token;
  4. registration with that token creates an approved test account linked to the I-STEM user, which can sign in;
  5. the same I-STEM account cannot be registered twice.

Steps 1-5 run in one database transaction that is rolled back: no portal account, link or email remains. The only
lasting change is the fictitious user on I-STEM staging (step 0), whose id is printed.

Usage (inside the django container): python - < scripts/ops/e2e_istem_registration.py
"""

import os
import re
import sys
import uuid

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from datetime import timedelta  # noqa: E402
from unittest.mock import patch  # noqa: E402

from celery.app.task import Task  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.communication.service import CommunicationService  # noqa: E402
from iic_booking.istem import registration_lookup as reg  # noqa: E402
from iic_booking.istem.models import IstemUserLink  # noqa: E402
from iic_booking.istem.service import default_client  # noqa: E402
from iic_booking.users.models import Department  # noqa: E402
from iic_booking.users.models.department import DepartmentType  # noqa: E402
from iic_booking.users.models.user_type import UserType  # noqa: E402

TAG = uuid.uuid4().hex[:6]
RESULTS = []
CODES = []  # OTPs the portal would have emailed


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


class _Rollback(Exception):
    pass


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def _call(method, path, body=None, user=None):
    client = APIClient(HTTP_HOST=_host())
    if user is not None:
        client.force_authenticate(user=user)
    res = client.get(path, secure=True) if method == "get" else client.post(path, body or {}, format="json", secure=True)
    data = getattr(res, "data", None)
    return res.status_code, (data if isinstance(data, dict) else {"data": data})


def _capture_otp(subject, message, from_email, recipient_list, **kwargs):
    found = re.search(r"(?<![0-9])([0-9]{6})(?![0-9])", message)
    CODES.append(found.group(1) if found else "")


def staging_user():
    """A fictitious I-STEM staging user (email, mobile, id)."""
    email = os.environ.get("E2E_ISTEM_EMAIL", "").strip().lower()
    client = default_client()
    if email:
        rows = client.lookup_public_user(user_email=email)
        return email, rows[0]["profile"].get("mobile", "") if rows else "", rows[0]["user_id"] if rows else None
    email = f"iic.e2e.{TAG}@example.com"
    mobile = "90000" + f"{uuid.uuid4().int % 100000:05d}"
    institute = str(getattr(settings, "ISTEM_INSTITUTE_ID", "") or "213")
    payload = {
        "user_first_name": "Iic", "user_last_name": "Etoe Test", "user_email": email, "user_contactno": mobile,
        "user_organisation": "IIC Portal Test", "user_type": "Industry User", "user_gender": "Other",
        "user_salutation": "Mr.", "user_address": "IIT Roorkee", "user_city": "Roorkee", "user_district": "Haridwar",
        "user_state": "Uttarakhand", "user_country": "India", "user_pin": "247667",
        "billing_user_name": "IIC Portal Test", "billing_address": "IIT Roorkee, Roorkee 247667",
        "institute_id": institute, "user_status": "1",
    }
    result = client.create_user(payload)
    print("INFO | I-STEM staging user created:", result.created, "| user_id:", result.user_id, "|", result.message)
    return email, mobile, result.user_id


def run(email, mobile, istem_user_id):
    key = str(getattr(settings, "ISTEM_REGISTRATION_TEST_KEY", "") or "")
    check("registration test key is set", bool(key))
    code, data = _call("get", "/api/istem/register/availability/")
    check("not offered without the test key", code == 200 and data.get("available") is False, f"status={code} {data}")
    code, data = _call("get", f"/api/istem/register/availability/?istem_test={key}")
    check("offered with the test key", code == 200 and data.get("available") is True, f"status={code}")

    code, data = _call("post", "/api/istem/register/lookup/", {"email": email, "istem_test": key})
    token = data.get("token")
    check("look-up finds the I-STEM account and sends an OTP", code == 200 and token and len(CODES) == 1,
          f"status={code} code={data.get('code')} message={data.get('message') or data.get('error')}")
    if not token:
        return
    wrong = "000000" if CODES[0] != "000000" else "111111"
    code, data = _call("post", "/api/istem/register/verify/", {"token": token, "otp": wrong})
    check("a wrong OTP is refused", code == 400 and data.get("code") == "wrong_code", f"status={code} code={data.get('code')}")
    code, data = _call("post", "/api/istem/register/verify/", {"token": token, "otp": CODES[0]})
    link_token, profile = data.get("link_token"), data.get("profile") or {}
    check("the right OTP returns the I-STEM profile", code == 200 and link_token and profile.get("email") == email,
          f"status={code} code={data.get('code')} otp_len={len(CODES[0])} profile_keys={sorted(profile)[:8]}")
    if not link_token:
        return

    org = Department.objects.create(name=f"ZZ I-STEM E2E {TAG}", code=f"ZZI{TAG[:4].upper()}",
                                    department_type=DepartmentType.EXTERNAL)
    password = "E2e-" + uuid.uuid4().hex[:12] + "!A1"
    code, data = _call("post", "/api/auth/register/", {
        "email": email, "password": password, "password_confirm": password, "name": "Iic Etoe Test",
        "gender": "other", "phone_number": mobile or "9000000000", "user_type": UserType.EXTERNAL,
        "department": org.pk, "program_end_date": (timezone.localdate() + timedelta(days=200)).isoformat(),
        "istem_link_token": link_token,
    })
    check("registration is confirmed at once and linked", code == 201 and data.get("registration_confirmed") is True
          and data.get("istem_linked") is True, f"status={code} {str(data)[:200]}")
    user = get_user_model().objects.filter(email__iexact=email).first()
    link = IstemUserLink.objects.filter(user=user).first() if user else None
    check("the account is approved, active and a test account",
          bool(user and user.email_verified and user.admin_approved and user.is_active and user.is_test_account),
          f"user={'yes' if user else 'no'}")
    check("linked to the I-STEM user", bool(link and str(link.public_user_id) == str(istem_user_id or link.public_user_id)),
          f"link_public_user_id={getattr(link, 'public_user_id', None)} expected={istem_user_id}")
    code, data = _call("post", "/api/auth/login/", {"email": email, "password": password})
    check("the new account can sign in", code == 200, f"status={code}")
    code, data = _call("post", "/api/istem/register/lookup/", {"email": email, "istem_test": key})
    check("the same I-STEM account cannot be registered twice", code == 409 and data.get("code") == "already_registered",
          f"status={code} code={data.get('code')}")


def main():
    print("TAG", TAG)
    try:
        email, mobile, istem_user_id = staging_user()
    except Exception as exc:  # noqa: BLE001
        check("fictitious I-STEM staging user available", False, f"{type(exc).__name__}: {exc}")
        email = None
    if email:
        check("fictitious I-STEM staging user available", bool(istem_user_id), f"user_id={istem_user_id}")
        silent = lambda *a, **k: None  # noqa: E731
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
                patch.object(Task, "apply_async", silent), \
                patch.object(reg, "send_mail", _capture_otp), \
                patch.object(CommunicationService, "send_email", silent), \
                patch.object(CommunicationService, "send_email_with_attachments", silent), \
                patch.object(CommunicationService, "send_sms", silent), \
                patch.object(CommunicationService, "send_push_notification", silent):
            try:
                with transaction.atomic():
                    try:
                        run(email, mobile, istem_user_id)
                    except Exception as exc:
                        import traceback

                        traceback.print_exc()
                        check("test ran without an unexpected error", False, f"{type(exc).__name__}: {exc}")
                    raise _Rollback()
            except _Rollback:
                print("ROLLED BACK: no portal account, link or email remains")
            from django.core import mail

            held = [m.subject for m in getattr(mail, "outbox", [])]
            print("INFO | emails held in memory, not sent:", len(held), held)
        left = get_user_model().objects.filter(email__iexact=email).count()
        check("cleanup: no portal account left", left == 0, f"accounts={left}")
        print("INFO | I-STEM staging test user stays on staging: user_id", istem_user_id, "email", email)
    failed = [n for n, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()
