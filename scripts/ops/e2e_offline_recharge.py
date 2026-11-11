"""
End-to-end check on production of the external offline-payment wallet recharge (Wallet -> Offline payments).

Acts only as flagged test accounts against the real endpoints:
  * an external test user submits offline payments with a receipt (PDF and camera JPEG);
  * a test Department Administrator / Accounts In Charge approves, rejects and reconciles within scope;
  * out-of-scope users and the requester are refused; the legacy endpoints refuse the new mode;
  * approval credits the wallet exactly once.

Every row it creates or changes (requests, receipts, wallet credit, audit, notifications) lives inside one database
transaction that is rolled back at the end, and the receipt files it stored are deleted. Emails go to the in-memory
backend and are counted (test vs real recipients) but never sent; push notifications and Celery tasks are not sent.
Prints ids, counts and PASS/FAIL only (public run log).

Usage (inside the django container): python - < scripts/ops/e2e_offline_recharge.py
"""

import os
import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from celery.app.task import Task  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core import mail  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.users.models import SubWallet, UserType, WalletRechargeRequest  # noqa: E402
from iic_booking.users.models.offline_recharge import OfflineRechargePayment  # noqa: E402

User = get_user_model()
BASE = "/api/wallet/offline-recharge/"
TAG = uuid.uuid4().hex[:6].upper()
PDF = b"%PDF-1.4\n% e2e offline recharge receipt\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01e2e"
RESULTS = []
STORED_FILES = []
PUSHES = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


def info(name, detail=""):
    print("INFO", "|", name, "|", detail)


class _Rollback(Exception):
    pass


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def client(user=None):
    c = APIClient(HTTP_HOST=_host())
    if user is not None:
        c.force_authenticate(user=user)
    return c


def call(c, method, path, body=None, multipart=False):
    if method == "get":
        res = c.get(path, secure=True)
    else:
        res = getattr(c, method)(path, body or {}, format="multipart" if multipart else "json", secure=True)
    data = getattr(res, "data", None)
    if data is None:
        try:
            data = res.json()
        except Exception:
            data = {}
    return res.status_code, data if isinstance(data, dict) else {"data": data}


def test_user(user_type):
    return User.objects.filter(is_test_account=True, is_active=True, user_type=user_type).order_by("pk").first()


def balance(wallet, dept_id):
    sw = SubWallet.objects.filter(wallet=wallet, department_id=dept_id).first()
    return sw.balance if sw else Decimal("0.00")


def recipients_summary(outbox, pushes):
    emails = set()
    for m in outbox:
        emails.update((a or "").strip().lower() for a in list(m.to) + list(m.cc) + list(m.bcc))
    emails.discard("")
    users = {u.email.lower(): u for u in User.objects.filter(email__in=list(emails))} if emails else {}
    test_mail = sum(1 for e in emails if e in users and users[e].is_test_account)
    real_mail = sum(1 for e in emails if e in users and not users[e].is_test_account)
    other_mail = len(emails) - test_mail - real_mail
    push_ids = set(pushes)
    test_push = User.objects.filter(pk__in=push_ids, is_test_account=True).count()
    real_push = len(push_ids) - test_push
    return test_mail, real_mail, other_mail, test_push, real_push


def form(dept_id, reference, *, file=None, mode="NEFT", amount="150"):
    return {
        "amount": amount,
        "department_id": str(dept_id),
        "payment_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
        "payment_mode": mode,
        "transaction_reference": reference,
        "payer_bank": "E2E Test Bank",
        "payer_account_name": "E2E Test Payer",
        "paid_into": "E2E test",
        "remarks": "Automated e2e check - rolled back",
        "client_request_id": f"e2e-{TAG}-{reference}",
        "receipt_file": file or SimpleUploadedFile("receipt.pdf", PDF, content_type="application/pdf"),
    }


def _record_push(*args, **kwargs):
    user = kwargs.get("recipient") or (args[0] if args else None)
    pk = getattr(user, "pk", None)
    if pk:
        PUSHES.append(pk)
    return None


def run():
    external = test_user(UserType.EXTERNAL)
    dept_admin = test_user(UserType.DEPT_ADMIN)
    accounts = test_user(UserType.FINANCE)
    info("test accounts", f"external={getattr(external, 'pk', None)} dept_admin={getattr(dept_admin, 'pk', None)} "
         f"accounts={getattr(accounts, 'pk', None)}")
    if external is None or dept_admin is None:
        check("test accounts present", False, "need an external and a dept_admin test account")
        return
    info("rollout", f"EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS={settings.EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS}")

    ext = client(external)
    status_, data = call(ext, "get", BASE)
    check("external test user sees the offline payment option", status_ == 200 and data.get("available") is True,
          f"status={status_} available={data.get('available')}")

    _, depts = call(ext, "get", "/api/wallet/departments-for-recharge/")
    offered = [d.get("id") for d in (depts.get("departments") or [])]
    dept_id = dept_admin.department_id
    check("dept admin's department is offered for recharge", dept_id in offered, f"dept={dept_id} offered={len(offered)}")
    if dept_id not in offered:
        return
    wallet = external.get_accessible_wallet()
    start = balance(wallet, dept_id)
    from iic_booking.external_services.models import ExternalNotification

    first_notice = (ExternalNotification.objects.order_by("-pk").values_list("pk", flat=True).first() or 0)

    # 1. Submit (PDF), idempotent resubmit, duplicate UTR refused.
    ref_a = f"E2EA{TAG}"
    s1, d1 = call(ext, "post", BASE, form(dept_id, ref_a), multipart=True)
    check("submit with PDF receipt", s1 == 201, f"status={s1} code={d1.get('code')}")
    req_a = (d1.get("request") or {}).get("id")
    if not req_a:
        return
    detail = OfflineRechargePayment.objects.select_related("receipt").get(recharge_request_id=req_a)
    if detail.receipt and detail.receipt.receipt_file:
        STORED_FILES.append(detail.receipt.receipt_file.name)
    check("new request is Pending approval", (d1.get("request") or {}).get("state") == "pending")

    s1b, d1b = call(ext, "post", BASE, form(dept_id, ref_a), multipart=True)
    check("same client id returns the same request", s1b in (200, 201) and (d1b.get("request") or {}).get("id") == req_a,
          f"status={s1b} created={d1b.get('created')}")
    dup = form(dept_id, ref_a)
    dup["client_request_id"] = f"e2e-{TAG}-dup"
    s_dup, d_dup = call(ext, "post", BASE, dup, multipart=True)
    check("duplicate UTR refused", s_dup == 409 and d_dup.get("code") == "duplicate_reference", f"status={s_dup}")

    # 2. Scope: requester, other-department accounts, anonymous and legacy endpoints are refused.
    s_self, _ = call(ext, "post", f"{BASE}{req_a}/approve/", {"note": "self"})
    check("requester cannot approve", s_self in (403, 404), f"status={s_self}")
    if accounts is not None and accounts.department_id != dept_id:
        s_out, _ = call(client(accounts), "post", f"{BASE}{req_a}/approve/", {"note": "out of scope"})
        check("other department's Accounts In Charge refused", s_out in (403, 404), f"status={s_out}")
    s_anon, _ = call(client(), "post", f"/api/wallet/recharge-requests/{req_a}/approve/", {})
    check("legacy approve refuses anonymous", s_anon in (401, 403), f"status={s_anon}")
    s_leg, _ = call(client(dept_admin), "post", f"/api/wallet/recharge-requests/{req_a}/approve/", {})
    check("legacy approve refuses the offline mode", s_leg in (403, 409), f"status={s_leg}")
    check("nothing credited before approval", balance(wallet, dept_id) == start)

    # 3. Approver queue and approval (lower amount), single credit.
    da = client(dept_admin)
    s_q, d_q = call(da, "get", f"{BASE}queue/?state=pending")
    check("request in the dept admin's queue", s_q == 200 and any(r.get("id") == req_a for r in d_q.get("requests") or []),
          f"status={s_q} role={d_q.get('role')}")
    s_nn, _ = call(da, "post", f"{BASE}{req_a}/approve/", {"note": ""})
    check("approval without a note refused", s_nn == 400, f"status={s_nn}")
    s_hi, _ = call(da, "post", f"{BASE}{req_a}/approve/", {"note": "too much", "approved_amount": "999"})
    check("approval above the declared amount refused", s_hi == 400, f"status={s_hi}")
    s_ap, d_ap = call(da, "post", f"{BASE}{req_a}/approve/", {"note": "E2E bank shows 120", "approved_amount": "120"})
    check("approve with note and lower amount", s_ap == 200, f"status={s_ap} state={(d_ap.get('request') or {}).get('state')}")
    s_ap2, _ = call(da, "post", f"{BASE}{req_a}/approve/", {"note": "again"})
    check("second approval refused", s_ap2 in (400, 409), f"status={s_ap2}")
    credited = balance(wallet, dept_id) - start
    check("wallet credited exactly once with the approved amount", credited == Decimal("120.00"), f"credited={credited}")
    req_obj = WalletRechargeRequest.objects.get(pk=req_a)
    check("one wallet transaction linked", bool(OfflineRechargePayment.objects.get(recharge_request=req_obj).wallet_transaction_id))

    # 4. Reconciliation: mismatch, then reconciled; reconciled is final.
    rec = client(accounts) if accounts is not None and accounts.department_id == dept_id else da
    s_mm, d_mm = call(rec, "post", f"{BASE}{req_a}/reconcile/", {"outcome": "mismatch", "note": "E2E amount differs"})
    expected_mm = 200 if rec is not da else s_mm
    check("flag mismatch", s_mm == expected_mm and (rec is da or (d_mm.get("request") or {}).get("state") == "mismatch"),
          f"status={s_mm} reconciler={'accounts' if rec is not da else 'dept_admin'}")
    if rec is not da:
        s_rc, d_rc = call(rec, "post", f"{BASE}{req_a}/reconcile/", {
            "outcome": "reconciled", "reference": f"E2E-CR-{TAG}",
            "credit_date": (timezone.localdate() - timedelta(days=1)).isoformat(), "note": ""})
        check("mark reconciled", s_rc == 200 and (d_rc.get("request") or {}).get("state") == "reconciled", f"status={s_rc}")
        s_rc2, _ = call(rec, "post", f"{BASE}{req_a}/reconcile/", {"outcome": "mismatch", "note": "after"})
        check("reconciled is final", s_rc2 in (400, 409), f"status={s_rc2}")
    else:
        info("reconcile", "no Accounts In Charge test account in this department: dept admin cannot reconcile "
             f"(status={s_mm}, expected 403)")

    # 5. Reject path (camera JPEG) and cancel path.
    photo = SimpleUploadedFile("IMG_E2E.jpg", JPEG, content_type="image/jpeg")
    s_b, d_b = call(ext, "post", BASE, form(dept_id, f"E2EB{TAG}", file=photo, mode="UPI"), multipart=True)
    check("submit with camera photo", s_b == 201, f"status={s_b}")
    req_b = (d_b.get("request") or {}).get("id")
    if req_b:
        rb = OfflineRechargePayment.objects.select_related("receipt").get(recharge_request_id=req_b)
        if rb.receipt and rb.receipt.receipt_file:
            STORED_FILES.append(rb.receipt.receipt_file.name)
        s_rj0, _ = call(da, "post", f"{BASE}{req_b}/reject/", {"note": ""})
        check("reject without reason refused", s_rj0 == 400, f"status={s_rj0}")
        s_rj, d_rj = call(da, "post", f"{BASE}{req_b}/reject/", {"note": "E2E receipt unreadable"})
        check("reject with reason", s_rj == 200 and (d_rj.get("request") or {}).get("state") == "rejected", f"status={s_rj}")
        _, mine = call(ext, "get", BASE)
        row = next((r for r in mine.get("requests") or [] if r.get("id") == req_b), {})
        check("user sees the rejection reason", row.get("decision_note") == "E2E receipt unreadable")
        again = form(dept_id, f"E2EB{TAG}")
        again["client_request_id"] = f"e2e-{TAG}-resubmit"
        s_again, _ = call(ext, "post", BASE, again, multipart=True)
        check("rejected UTR can be submitted again", s_again == 201, f"status={s_again}")
        if s_again == 201:
            rid = OfflineRechargePayment.objects.filter(transaction_reference__icontains=f"E2EB{TAG}").exclude(
                recharge_request_id=req_b).select_related("receipt").first()
            if rid and rid.receipt and rid.receipt.receipt_file:
                STORED_FILES.append(rid.receipt.receipt_file.name)
            if rid:
                s_c, _ = call(ext, "post", f"{BASE}{rid.recharge_request_id}/cancel/", {})
                check("user cancels a pending request", s_c == 200, f"status={s_c}")
    check("rejected and cancelled requests credit nothing", balance(wallet, dept_id) - start == Decimal("120.00"))

    # 6. Notifications: queued rows (sent only on commit, so never sent here) and any direct email (in memory).
    rows = ExternalNotification.objects.filter(pk__gt=first_notice).select_related("recipient")
    users = {}
    for row in rows:
        users.setdefault(row.recipient_id, row.recipient)
    test_ids = sorted(pk for pk, u in users.items() if u.is_test_account)
    real = [u for u in users.values() if not u.is_test_account]
    info("notifications queued", f"rows={rows.count()} test_recipient_ids={test_ids} real_recipients={len(real)} "
         f"real_recipient_types={sorted({str(u.user_type) for u in real})}")
    test_mail, real_mail, other_mail, test_push, real_push = recipients_summary(mail.outbox, PUSHES)
    info("direct emails", f"messages={len(mail.outbox)} test={test_mail} real={real_mail} non_user={other_mail}")
    check("requester notified", external.pk in users)
    check("test approver notified of the new request", dept_admin.pk in users)
    check("no real (non-test) user would be notified about a test request", not real and real_mail == 0 and real_push == 0,
          f"real_recipients={len(real)} real_mail={real_mail} real_push={real_push}")

def sweep_leftover_e2e_receipts():
    """Delete receipt files earlier runs of this script left behind (a run that crashed before its own cleanup).
    Only files in the receipt folders for yesterday, today and tomorrow whose bytes are exactly this script's
    test receipt are touched."""
    from django.core.files.storage import default_storage

    removed = 0
    today = timezone.now().date()
    for offset in (-1, 0, 1):
        folder = (today + timedelta(days=offset)).strftime("wallet_payment_receipts/%Y/%m/%d")
        try:
            _, files = default_storage.listdir(folder)
        except Exception:  # noqa: BLE001 - folder missing or storage cannot list
            continue
        for fname in files:
            if not (fname.startswith("receipt") or fname.startswith("IMG_E2E")):
                continue
            name = f"{folder}/{fname}"
            try:
                if default_storage.size(name) not in (len(PDF), len(JPEG)):
                    continue
                with default_storage.open(name, "rb") as fh:
                    content = fh.read()
                if content in (PDF, JPEG):
                    default_storage.delete(name)
                    removed += 1
            except Exception as exc:  # noqa: BLE001
                print("WARN | could not check a stored receipt |", type(exc).__name__)
    info("leftover e2e receipts deleted", str(removed))


def main():
    sweep_leftover_e2e_receipts()
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", lambda *a, **k: None), \
            patch("iic_booking.communication.service.CommunicationService.send_push_notification", _record_push):
        mail.outbox = []
        try:
            with transaction.atomic():
                try:
                    run()
                finally:
                    from django.core.files.storage import default_storage

                    for name in STORED_FILES:
                        try:
                            default_storage.delete(name)
                        except Exception as exc:  # noqa: BLE001
                            print("WARN | could not delete stored receipt |", type(exc).__name__)
                    info("stored receipts deleted", str(len(STORED_FILES)))
                raise _Rollback()
        except _Rollback:
            info("rolled back", "every row created by this run was discarded")
    failed = [n for n, ok in RESULTS if not ok]
    print(f"SUMMARY | passed={len(RESULTS) - len(failed)} failed={len(failed)}")
    for n in failed:
        print("FAILED |", n)
    sys.exit(1 if failed else 0)


main()
