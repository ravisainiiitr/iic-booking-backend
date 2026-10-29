"""Invoice preference, SRIC request queue, working-day batch, upload and secure download. Email goes to Django's
in-memory test backend (``mail.outbox``); nothing is sent anywhere."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from iic_booking.equipment.models import Holiday
from iic_booking.external_services import invoices, sric_dispatch
from iic_booking.external_services.models import (
    ExternalServicesSettings,
    InvoiceRequestStatus,
    OfficialInvoiceDocument,
    PolicyText,
    SricDispatchAttempt,
    SricInvoiceBatch,
    SricInvoiceRequest,
)
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, main_admin, make_user

pytestmark = pytest.mark.django_db
IST = ZoneInfo("Asia/Kolkata")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def on():
    s = ExternalServicesSettings.load()
    s.invoice_requests_enabled = True
    s.save()
    return s


def _billing(user, **extra):
    from iic_booking.users.models import ExternalBillingProfile

    values = {"billing_name": "Asha Labs Pvt Ltd", "gstin": "09AAAAA0000A1Z5", "billing_address_line1": "12 Station Road",
              "billing_city": "Roorkee", "billing_state": "Uttarakhand", "billing_pincode": "247667"}
    values.update(extra)
    return ExternalBillingProfile.objects.update_or_create(user=user, defaults=values)[0]


@pytest.fixture
def paid(factory):
    """A completed, paid external booking with billing details."""
    user = make_user()
    _billing(user)
    eq = factory.equipment()
    booking = factory.booking(user, eq, factory.at(timezone.localdate() - timedelta(days=2)), status="COMPLETED",
                              completed_at=timezone.now() - timedelta(hours=3), payment_settled_at=timezone.now(),
                              charge_breakdown=[{"description": "Analysis", "amount": 100.0},
                                                {"description": "GST (18%)", "amount": 18.0}])
    return booking


def _finance(booking):
    return make_user(UserType.FINANCE, department=booking.equipment.internal_department, is_staff=True)


def _url(booking, suffix=""):
    return f"/api/v1/external-booking/bookings/{booking.pk}/invoice/{suffix}"


# --- preference -----------------------------------------------------------------------------------------------------

def test_disabled_by_default(paid):
    r = client_for(paid.user).post(_url(paid), {"option": "after_analysis"}, format="json")
    assert r.status_code == 403 and r.json()["code"] == "invoices_disabled"


def test_after_analysis_preference_then_completion_queues_once(on, paid):
    r = client_for(paid.user).post(_url(paid), {"option": "after_analysis"}, format="json")
    assert r.status_code == 200 and r.json()["preference"]["option"] == "after_analysis"
    assert invoices.queue_completed() == {"queued": 1, "skipped": 0, "incomplete": 0}
    assert invoices.queue_completed()["queued"] == 0  # a repeated completion event changes nothing
    req = SricInvoiceRequest.objects.get(booking=paid)
    assert req.trigger == "completion" and req.status == InvoiceRequestStatus.QUEUED
    snap = req.snapshot
    assert snap["billing"]["billing_name"] == "Asha Labs Pvt Ltd" and snap["billing"]["gstin"] == "09AAAAA0000A1Z5"
    assert snap["charges"]["total"] == f"{paid.total_charge:.2f}" and snap["trigger"] == "completion"


def test_no_preference_is_not_queued_unless_configured(on, paid):
    assert invoices.queue_completed()["queued"] == 0
    on.auto_request_without_preference = True
    on.save()
    assert invoices.queue_completed()["queued"] == 1


def test_unpaid_or_incomplete_billing_is_not_queued(on, factory):
    user = make_user()
    eq = factory.equipment()
    booking = factory.booking(user, eq, factory.at(timezone.localdate() - timedelta(days=1)), status="COMPLETED",
                              completed_at=timezone.now(), amount_due=Decimal("50.00"))
    client_for(user).post(_url(booking), {"option": "after_analysis"}, format="json")
    assert invoices.queue_completed()["queued"] == 0  # payment not realised
    booking.payment_settled_at = timezone.now()
    booking.save(update_fields=["payment_settled_at"])
    assert invoices.queue_completed()["incomplete"] == 1  # no billing profile: never sent half-filled
    _billing(user)
    assert invoices.queue_completed()["queued"] == 1


def test_early_option_needs_the_setting_and_the_current_disclosure(on, factory):
    user = make_user()
    _billing(user)
    booking = factory.booking(user, factory.equipment(), factory.at(timezone.localdate() + timedelta(days=5)),
                              payment_settled_at=timezone.now())
    r = client_for(user).post(_url(booking), {"option": "early"}, format="json")
    assert r.status_code == 403 and r.json()["code"] == "early_invoice_disabled"
    on.early_invoice_allowed = True
    on.save()
    r = client_for(user).post(_url(booking), {"option": "early"}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "acknowledgement_required"
    disclosure = PolicyText.objects.filter(key="early_invoice").order_by("-version").first()
    assert disclosure is not None and disclosure.approved is False
    r = client_for(user).post(_url(booking), {"option": "early", "early_invoice_ack_version": disclosure.version},
                              format="json")
    assert r.status_code == 200 and r.json()["preference"]["acknowledged_at"]
    r = client_for(user).post(_url(booking, "request-early/"), format="json")
    assert r.status_code == 201, r.content
    req = SricInvoiceRequest.objects.get(booking=booking)
    assert req.trigger == "early" and req.early_requested
    assert req.snapshot["early_acknowledgement"]["policy_version"] == disclosure.version
    # Completion later: the same request stands (one obligation per booking).
    booking.status, booking.completed_at = "COMPLETED", timezone.now()
    booking.save(update_fields=["status", "completed_at"])
    invoices.queue_completed()
    assert SricInvoiceRequest.objects.filter(booking=booking).count() == 1


def test_early_request_refused_before_payment(on, factory):
    on.early_invoice_allowed = True
    on.save()
    user = make_user()
    _billing(user)
    booking = factory.booking(user, factory.equipment(), factory.at(timezone.localdate() + timedelta(days=5)),
                              status="PENDING_PAYMENT", amount_due=Decimal("100.00"))
    version = PolicyText.objects.filter(key="early_invoice").order_by("-version").first().version
    client_for(user).post(_url(booking), {"option": "early", "early_invoice_ack_version": version}, format="json")
    r = client_for(user).post(_url(booking, "request-early/"), format="json")
    assert r.status_code == 400 and r.json()["code"] == "payment_pending"


def test_other_users_cannot_see_or_change_a_booking_invoice(on, paid):
    other = make_user()
    assert client_for(other).get(_url(paid)).status_code == 404
    assert client_for(other).post(_url(paid), {"option": "after_analysis"}, format="json").status_code == 404


# --- batch ----------------------------------------------------------------------------------------------------------

def _queue(on, paid):
    client_for(paid.user).post(_url(paid), {"option": "after_analysis"}, format="json")
    invoices.queue_completed()
    return SricInvoiceRequest.objects.get(booking=paid)


def _working_day_at(hour, minute=0):
    d = timezone.localdate() + timedelta(days=1)
    while d.weekday() >= 5 or Holiday.objects.filter(date=d, is_active=True).exists():
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def test_batch_sends_nothing_while_dispatch_is_off(on, paid):
    _queue(on, paid)
    assert sric_dispatch.run_batch(_working_day_at(9, 30)) == {"skipped": "off"}
    assert len(mail.outbox) == 0


def test_batch_rules_weekend_holiday_and_time(on, paid):
    on.sric_dispatch_mode, on.sric_test_recipient_email = "test", "sric-test@example.org"
    on.save()
    _queue(on, paid)
    saturday = _working_day_at(9, 30)
    while saturday.weekday() != 5:
        saturday += timedelta(days=1)
    assert sric_dispatch.run_batch(saturday) == {"skipped": "not_working_day"}
    workday = _working_day_at(9, 30)
    Holiday.objects.create(date=workday.date(), reason="Institute holiday", is_active=True)
    assert sric_dispatch.run_batch(workday) == {"skipped": "not_working_day"}
    Holiday.objects.filter(date=workday.date()).delete()
    assert sric_dispatch.run_batch(workday.replace(hour=9, minute=29)) == {"skipped": "before_0930"}


def test_batch_sends_each_request_once_in_test_mode(on, paid):
    on.sric_dispatch_mode, on.sric_test_recipient_email = "test", "sric-test@example.org"
    on.save()
    req = _queue(on, paid)
    SricInvoiceRequest.objects.filter(pk=req.pk).update(created_at=timezone.now() - timedelta(days=2))
    at = _working_day_at(9, 30)
    result = sric_dispatch.run_batch(at)
    assert result["batched"] == 1 and result["sent"] == 1
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["sric-test@example.org"] and msg.subject.startswith("[TEST] GST Invoice Request — ")
    assert "Asha Labs Pvt Ltd" in msg.body and "09AAAAA0000A1Z5" in msg.body
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.SENT and req.sent_in_mode == "test" and req.batch is not None
    # Repeated scheduler runs the same day: no second batch, no second email.
    assert sric_dispatch.run_batch(at + timedelta(minutes=30))["sent"] == 0
    assert SricInvoiceBatch.objects.count() == 1 and len(mail.outbox) == 1


def test_sent_is_not_invoice_available(on, paid):
    on.sric_dispatch_mode, on.sric_test_recipient_email = "test", "t@example.org"
    on.save()
    req = _queue(on, paid)
    SricInvoiceRequest.objects.filter(pk=req.pk).update(created_at=timezone.now() - timedelta(days=2))
    sric_dispatch.run_batch(_working_day_at(9, 30))
    status = client_for(paid.user).get(_url(paid)).json()
    assert status["stage"] == "with_sric" and status["document"] is None


def test_failures_retry_with_backoff_then_alert(on, paid, monkeypatch):
    on.sric_dispatch_mode, on.sric_test_recipient_email = "test", "t@example.org"
    on.save()
    finance = _finance(paid)
    req = _queue(on, paid)
    SricInvoiceRequest.objects.filter(pk=req.pk).update(created_at=timezone.now() - timedelta(days=2))

    def broken(self, fail_silently=False):
        raise ConnectionError("smtp down")

    monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", broken)
    at = _working_day_at(9, 30)
    assert sric_dispatch.run_batch(at)["failed"] == 1
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.FAILED and req.attempts == 1 and req.next_attempt_at > at
    assert sric_dispatch.run_batch(at + timedelta(minutes=5))["failed"] == 0  # backoff respected
    for i in range(1, sric_dispatch.MAX_ATTEMPTS):
        SricInvoiceRequest.objects.filter(pk=req.pk).update(next_attempt_at=at)
        sric_dispatch.run_batch(at + timedelta(hours=i))
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.GAVE_UP and req.attempts == sric_dispatch.MAX_ATTEMPTS
    from iic_booking.external_services.models import ExternalNotification

    assert ExternalNotification.objects.filter(key=f"invoice:{req.pk}:gave_up", recipient=finance).exists()
    assert SricDispatchAttempt.objects.filter(request=req, outcome="failed").count() == sric_dispatch.MAX_ATTEMPTS


def test_crash_after_claim_becomes_uncertain_and_is_not_resent(on, paid):
    on.sric_dispatch_mode, on.sric_test_recipient_email = "test", "t@example.org"
    on.save()
    req = _queue(on, paid)
    SricInvoiceRequest.objects.filter(pk=req.pk).update(status=InvoiceRequestStatus.SENDING,
                                                       claimed_at=timezone.now() - timedelta(hours=2))
    assert sric_dispatch.run_batch(_working_day_at(9, 30))["uncertain"] == 1
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.UNCERTAIN and len(mail.outbox) == 0
    finance = _finance(paid)
    r = client_for(finance).post(f"/api/v1/external-booking/staff/invoice-requests/{req.pk}/resolve/",
                                 {"action": "confirm_sent", "note": "SRIC confirmed by phone"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "sent"


# --- upload and download ---------------------------------------------------------------------------------------------

def _sent(on, paid):
    req = _queue(on, paid)
    SricInvoiceRequest.objects.filter(pk=req.pk).update(status=InvoiceRequestStatus.SENT, sent_at=timezone.now())
    return req


def _upload(user, req, data=PDF, name="invoice.pdf", ref=None, **extra):
    return client_for(user).post(
        f"/api/v1/external-booking/staff/invoice-requests/{req.pk}/upload/",
        {"file": SimpleUploadedFile(name, data, content_type="application/pdf"),
         "booking_reference": ref if ref is not None else req.booking.virtual_booking_id, **extra},
        format="multipart",
    )


def test_only_accounts_in_charge_of_the_department_can_upload(on, paid, factory):
    req = _sent(on, paid)
    other_dept_finance = make_user(UserType.FINANCE, department=factory.__class__().department)
    assert _upload(other_dept_finance, req).status_code == 404
    assert _upload(paid.user, req).status_code == 404
    assert _upload(make_user(UserType.MANAGER), req).status_code == 404


def test_unauthorised_upload_is_refused_before_the_file_is_validated(on, paid):
    req = _sent(on, paid)
    for user in (paid.user, make_user(UserType.OPERATOR)):
        r = _upload(user, req, data=b"not a pdf")
        assert r.status_code == 404 and r.json()["code"] == "not_found", r.content
        r = client_for(user).post(f"/api/v1/external-booking/staff/invoice-requests/{req.pk}/upload/", {},
                                  format="multipart")
        assert r.status_code == 404, r.content


def test_upload_validates_file_and_reference(on, paid):
    req = _sent(on, paid)
    finance = _finance(paid)
    assert _upload(finance, req, data=b"not a pdf").json()["field"] == "file"
    assert _upload(finance, req, name="invoice.exe").json()["field"] == "file"
    r = _upload(finance, req, ref="IICWRONG123")
    assert r.status_code == 400 and r.json()["code"] == "reference_mismatch"
    r = _upload(finance, req, invoice_number="SRIC/2026/123")
    assert r.status_code == 201, r.content
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.INVOICE_RECEIVED
    assert _upload(finance, req).json()["code"] == "already_uploaded"


def test_replacement_keeps_history(on, paid):
    req = _sent(on, paid)
    finance = _finance(paid)
    first = _upload(finance, req).json()["document_id"]
    r = _upload(finance, req, data=PDF + b"%corrected", replace="true", replacement_reason="Wrong GSTIN on first copy")
    assert r.status_code == 201, r.content
    docs = OfficialInvoiceDocument.objects.filter(request=req).order_by("pk")
    assert [d.is_current for d in docs] == [False, True]
    assert docs[1].replaces_id == first and docs[1].replacement_reason


def test_user_downloads_only_their_current_invoice(on, paid):
    req = _sent(on, paid)
    finance = _finance(paid)
    doc_id = _upload(finance, req).json()["document_id"]
    url = f"/api/v1/external-booking/invoices/documents/{doc_id}/download/"
    r = client_for(paid.user).get(url)
    assert r.status_code == 200 and b"".join(r.streaming_content).startswith(b"%PDF")
    assert r["Cache-Control"] == "no-store"
    assert client_for(make_user()).get(url).status_code == 404
    assert client_for(finance).get(url).status_code == 200
    status = client_for(paid.user).get(_url(paid)).json()
    assert status["stage"] == "invoice_available" and status["document"]["id"] == doc_id


def test_upload_notifies_the_user_once(on, paid):
    from iic_booking.external_services.models import ExternalNotification

    req = _sent(on, paid)
    finance = _finance(paid)
    doc_id = _upload(finance, req).json()["document_id"]
    key = f"invoice:{req.pk}:document:{doc_id}"
    assert ExternalNotification.objects.filter(key=key, recipient=paid.user).count() == 2  # in-portal + email
    invoices.notifications.notify(key, [paid.user], title="again", message="again")  # a retried event
    assert ExternalNotification.objects.filter(key=key, recipient=paid.user).count() == 2


def test_staff_queue_is_scoped_and_searchable(on, paid):
    _queue(on, paid)
    finance = _finance(paid)
    rows = client_for(finance).get("/api/v1/external-booking/staff/invoice-requests/",
                                   {"q": paid.virtual_booking_id}).json()["requests"]
    assert [r["booking_id"] for r in rows] == [paid.pk]
    assert client_for(paid.user).get("/api/v1/external-booking/staff/invoice-requests/").status_code == 403
    admin_rows = client_for(main_admin()).get("/api/v1/external-booking/staff/invoice-requests/").json()["requests"]
    assert paid.pk in [r["booking_id"] for r in admin_rows]


def test_next_batch_time_skips_weekends():
    friday_evening = datetime(2026, 11, 13, 18, 0, tzinfo=IST)  # a Friday
    assert sric_dispatch.next_batch_at(friday_evening) == datetime(2026, 11, 16, 9, 30, tzinfo=IST)
    assert date(2026, 11, 14).weekday() == 5


def test_provisional_invoice_is_labelled_and_access_controlled(on, paid):
    url = f"/api/v1/external-booking/bookings/{paid.pk}/provisional-invoice/"
    r = client_for(paid.user).get(url)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert client_for(make_user()).get(url).status_code == 404
    assert client_for(_finance(paid)).get(url).status_code == 200
