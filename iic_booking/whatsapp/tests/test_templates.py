"""WhatsApp template management: catalogue rules, sync, submission, statuses, the send gate and event mapping. No network."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from django.core.management import CommandError, call_command
from django.utils import timezone

from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory
from iic_booking.whatsapp import hooks, service, templates
from iic_booking.whatsapp.models import WhatsAppMessage, WhatsAppOptIn, WhatsAppTemplate, WhatsAppTemplateAudit
from iic_booking.whatsapp.template_catalogue import BY_EVENT, CATALOGUE, FOOTER_STOP, validate
from iic_booking.whatsapp.template_client import TemplateApiError, TemplateClient, clean

pytestmark = pytest.mark.django_db

TOKEN = "EAAFakeTemplateToken123456789"
WABA = "3000000000000333"
TEMPLATE_ID = "9000000000000999"
NAME = "Indian Institute of Technology Roorkee"
EXISTING = ("iic_booking_confirmed", "iic_booking_cancelled", "iic_booking_completed")
EXPECTED_EVENTS = {
    "booking_confirmed", "booking_rejected", "booking_rescheduled", "booking_cancelled", "booking_reminder", "booking_completed",
    "payment_successful", "payment_failed", "refund_completed", "waitlist_slot_available", "otp_code", "remote_analysis_ready",
    "remote_analysis_results_uploaded",
}


class Resp:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


def meta_row(template, status="PENDING", **extra):
    components = [{"type": p["type"], **({"text": p["text"]} if "text" in p else {})} for p in template.payload()["components"]]
    return {"id": TEMPLATE_ID, "name": template.name, "language": template.language, "status": status, "category": template.category,
            "components": components, **extra}


class FakeMeta:
    """Stands in for requests. Any call that reaches a real network would fail the test instead."""

    def __init__(self, rows=None, waba_name=NAME, post=None, get_error=None):
        self.rows, self.waba, self.post_answers, self.get_error = list(rows or []), waba_name, list(post or []), get_error
        self.gets, self.posts = [], []

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers == {"Authorization": f"Bearer {TOKEN}"} and TOKEN not in url and TOKEN not in str(params)
        self.gets.append((url, params))
        if url.endswith(f"/{WABA}"):
            return Resp({"id": WABA, "name": self.waba})
        if self.get_error:
            return self.get_error
        return Resp({"data": self.rows})

    def post(self, url, json=None, headers=None, timeout=None):
        assert headers == {"Authorization": f"Bearer {TOKEN}"} and "messages" not in url
        self.posts.append((url, json))
        if self.post_answers:
            answer = self.post_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return Resp({"id": TEMPLATE_ID, "status": "PENDING", "category": "UTILITY"})


@pytest.fixture
def conf_on(settings):
    settings.WHATSAPP_WABA_ID = WABA
    settings.WHATSAPP_WABA_EXPECTED_NAME = NAME
    settings.WHATSAPP_ACCESS_TOKEN = TOKEN
    settings.WHATSAPP_API_VERSION = "v21.0"
    settings.WHATSAPP_APP_SECRET = "fake-app-secret"
    return settings


@pytest.fixture
def sending(conf_on):
    conf_on.WHATSAPP_ENABLED = True
    conf_on.WHATSAPP_PHONE_NUMBER_ID = "1000000000000111"
    conf_on.WHATSAPP_VERIFY_TOKEN = "fake-verify"
    conf_on.WHATSAPP_PILOT_NUMBERS = []
    conf_on.WHATSAPP_ALLOW_ALL = True
    return conf_on


def approve(*events):
    for event in events or BY_EVENT:
        t = BY_EVENT[event]
        WhatsAppTemplate.objects.update_or_create(waba_id=WABA, name=t.name, language=t.language, defaults={"status": "APPROVED", "event": event})


def opted_in():
    user = UserFactory(user_type=UserType.STUDENT, phone_number="9876543210")
    service.set_opt_in(user, opted_in=True, phone="+919876543210")
    return user


def run_command(meta, *args):
    out = StringIO()
    with patch("iic_booking.whatsapp.management.commands.whatsapp_sync_templates.TemplateClient", lambda: TemplateClient(session=meta)):
        call_command("whatsapp_sync_templates", *args, stdout=out)
    return out.getvalue()


# --- catalogue -------------------------------------------------------------------------------------------------------


def test_the_catalogue_covers_every_requested_event_and_meets_metas_rules():
    assert {t.event for t in CATALOGUE} == EXPECTED_EVENTS
    assert validate() == []


def test_every_entry_is_complete():
    for t in CATALOGUE:
        assert t.language == "en" and t.body and t.recipient
        if t.is_authentication:  # Meta's fixed wording, with the code as its only value: see test_otp.py
            continue
        assert t.category == "UTILITY" and t.header
        assert len(t.params) == len(t.examples) >= 3


def test_existing_templates_keep_metas_names_and_new_ones_offer_stop():
    assert {t.name for t in CATALOGUE if t.name.startswith("iic_")} == set(EXISTING)
    for t in CATALOGUE:
        if t.name not in EXISTING:
            assert t.name.startswith("iitr_") and (t.is_authentication or t.footer == FOOTER_STOP)


def test_the_payload_has_the_body_example_meta_requires():
    payload = BY_EVENT["payment_failed"].payload()
    assert payload["name"] == "iitr_payment_failed" and payload["category"] == "UTILITY" and payload["parameter_format"] == "positional"
    body = next(c for c in payload["components"] if c["type"] == "BODY")
    assert body["example"]["body_text"] == [list(BY_EVENT["payment_failed"].examples)]


def test_every_template_has_a_portal_step_and_the_unused_two_are_gone():
    assert len(CATALOGUE) == 13 and all(t.trigger for t in CATALOGUE)
    dropped = {"iitr_reschedule_requested", "iitr_reschedule_rejected", "iitr_reschedule_approved", "iitr_booking_request_received",
               "iitr_payment_initiated", "iitr_payment_pending", "iitr_refund_initiated", "iitr_sample_received"}
    assert not dropped & {t.name for t in CATALOGUE}


def test_wording_promises_nothing_the_backend_has_not_confirmed():
    assert "request" not in BY_EVENT["booking_rescheduled"].body.lower() and "approved" not in BY_EVENT["booking_rescheduled"].body.lower()
    assert "received the payment" not in BY_EVENT["payment_failed"].body
    assert "confirmed" not in BY_EVENT["payment_successful"].body.lower()
    assert "receipt" not in " ".join(t.body.lower() for t in CATALOGUE)


# --- dry run and submission ------------------------------------------------------------------------------------------


def test_dry_run_lists_exactly_what_would_go_and_changes_nothing(conf_on):
    meta = FakeMeta(rows=[meta_row(BY_EVENT["booking_confirmed"])])
    out = run_command(meta, "--dry-run")
    assert "Would submit: 12" in out and "SUBMIT iitr_payment_failed" in out and "SKIP iic_booking_confirmed [PENDING]" in out
    assert "SUBMIT iic_booking_confirmed" not in out
    assert meta.posts == [] and not WhatsAppTemplate.objects.exists() and not WhatsAppTemplateAudit.objects.exists()


def test_dry_run_is_the_default_and_can_show_wording(conf_on):
    meta = FakeMeta()
    out = run_command(meta, "--show-wording")
    assert "DRY RUN" in out and "BODY: Dear {{1}}," in out and "EXAMPLES:" in out and meta.posts == []
    assert "BODY (Meta's wording): {{1}} is your verification code." in out and "BUTTON: Copy code" in out


def test_submit_sends_only_missing_templates_in_a_batch_and_saves_the_id(conf_on):
    meta = FakeMeta(rows=[meta_row(BY_EVENT[e]) for e in ("booking_confirmed", "booking_cancelled", "booking_completed")])
    out = run_command(meta, "--submit", "--batch-size", "2", "--delay", "0")
    assert len(meta.posts) == 2 and "Submitted: 2" in out and "Still to submit: 8" in out
    assert all(url.endswith(f"/{WABA}/message_templates") for url, _ in meta.posts)
    assert not {body["name"] for _, body in meta.posts} & set(EXISTING)
    record = WhatsAppTemplate.objects.get(name=meta.posts[0][1]["name"])
    assert record.meta_id == TEMPLATE_ID and record.status == "PENDING" and record.submitted_at and record.submission_attempts == 1
    assert WhatsAppTemplateAudit.objects.filter(action="submitted").count() == 2


def test_pending_or_rejected_templates_at_meta_are_never_resubmitted(conf_on):
    rows = [meta_row(t, status="REJECTED" if i % 2 else "PENDING") for i, t in enumerate(CATALOGUE)]
    meta = FakeMeta(rows=rows)
    out = run_command(meta, "--submit", "--delay", "0")
    assert meta.posts == [] and "Submitted: 0" in out and "Skipped, already at Meta: 13" in out


def test_a_second_run_does_not_duplicate(conf_on):
    meta = FakeMeta()
    run_command(meta, "--submit", "--batch-size", "1", "--delay", "0")
    first = meta.posts[0][1]
    meta.rows.append(meta_row(next(t for t in CATALOGUE if t.name == first["name"])))
    run_command(meta, "--submit", "--batch-size", "1", "--delay", "0")
    assert meta.posts[1][1]["name"] != first["name"]


def test_batch_size_is_capped(conf_on):
    meta = FakeMeta()
    run_command(meta, "--submit", "--batch-size", "999", "--delay", "0")
    assert len(meta.posts) == templates.MAX_BATCH or len(meta.posts) == len(CATALOGUE)
    assert len(meta.posts) <= templates.MAX_BATCH


def test_the_wrong_account_is_refused_before_anything_is_sent(conf_on):
    meta = FakeMeta(waba_name="Test Messaging Account")
    with pytest.raises(CommandError, match="not the expected account"):
        run_command(meta, "--submit")
    assert meta.posts == []


def test_a_missing_waba_id_is_refused(conf_on):
    conf_on.WHATSAPP_WABA_ID = ""
    meta = FakeMeta()
    with pytest.raises(CommandError, match="WHATSAPP_WABA_ID is not set"):
        run_command(meta)
    assert meta.gets == [] and meta.posts == []


def test_a_missing_token_is_refused(conf_on):
    conf_on.WHATSAPP_ACCESS_TOKEN = ""
    with pytest.raises(CommandError, match="WHATSAPP_ACCESS_TOKEN is not set"):
        run_command(FakeMeta())


# --- API failures ----------------------------------------------------------------------------------------------------


def test_a_rate_limit_stops_the_batch_and_is_audited(conf_on):
    limit = Resp({"error": {"code": 80008, "message": "Too many"}}, 429)
    meta = FakeMeta(post=[Resp({"id": "1", "status": "PENDING"}), limit, Resp({"id": "2"})])
    with pytest.raises(CommandError, match="failed"):
        run_command(meta, "--submit", "--batch-size", "5", "--delay", "0")
    assert len(meta.posts) == 2
    failure = WhatsAppTemplateAudit.objects.get(action="submit_failed")
    assert failure.detail["kind"] == "rate_limit" and failure.http_status == 429 and not failure.ok


def test_a_refused_template_does_not_stop_the_others(conf_on):
    refused = Resp({"error": {"code": 100, "message": "Invalid parameter"}}, 400)
    meta = FakeMeta(post=[refused, Resp({"id": "2", "status": "PENDING"})])
    with pytest.raises(CommandError, match="1 template"):
        run_command(meta, "--submit", "--batch-size", "2", "--delay", "0")
    assert len(meta.posts) == 2
    assert WhatsAppTemplate.objects.filter(last_error__icontains="Invalid parameter").count() == 1


def test_a_duplicate_answer_is_recognised(conf_on):
    answer = Resp({"error": {"code": 100, "error_subcode": 2388024, "message": "Content in This Language Already Exists"}}, 400)
    with pytest.raises(TemplateApiError) as caught:
        TemplateClient(session=FakeMeta(post=[answer])).create_template({})
    assert caught.value.kind == "duplicate"


@pytest.mark.parametrize("answer, kind", [
    (Resp({"error": {"code": 190, "message": "expired"}}, 401), "token_invalid"),
    (Resp({"error": {"code": 200, "message": "no permission"}}, 403), "permission"),
    (Resp({}, 500), "server_error"),
    (Resp({"error": {"code": 4, "message": "limit"}}, 400), "rate_limit"),
])
def test_errors_are_classified(conf_on, answer, kind):
    with pytest.raises(TemplateApiError) as caught:
        TemplateClient(session=FakeMeta(get_error=answer)).list_templates()
    assert caught.value.kind == kind


def test_a_network_error_prints_only_its_type_and_stops(conf_on):
    meta = FakeMeta(post=[requests.ConnectionError(f"https://graph/x?access_token={TOKEN}"), Resp({"id": "2"})])
    with pytest.raises(CommandError):
        run_command(meta, "--submit", "--batch-size", "2", "--delay", "0")
    assert len(meta.posts) == 1
    assert TOKEN not in str(WhatsAppTemplateAudit.objects.get(action="submit_failed").detail)


def test_listing_follows_cursors_and_never_metas_next_link(conf_on):
    class Paged(FakeMeta):
        def get(self, url, params=None, headers=None, timeout=None):
            self.gets.append((url, params))
            if not params.get("after"):
                return Resp({"data": [{"name": "one"}], "paging": {"cursors": {"after": "CUR"}, "next": f"https://graph/x?access_token={TOKEN}"}})
            return Resp({"data": [{"name": "two"}], "paging": {"cursors": {"after": "END"}}})

    meta = Paged()
    assert [r["name"] for r in TemplateClient(session=meta).list_templates()] == ["one", "two"]
    assert all("access_token" not in url for url, _ in meta.gets)


# --- credential handling ---------------------------------------------------------------------------------------------


def test_clean_removes_tokens_ids_numbers_and_links(conf_on):
    text = clean(f"bad {TOKEN} EAAOtherToken12345678 waba {WABA} phone +91 98765 43210 see https://x.test/{TEMPLATE_ID}?t=1")
    for leak in (TOKEN, "EAAOther", WABA, "98765", TEMPLATE_ID, "x.test"):
        assert leak not in text


def test_no_output_or_stored_record_holds_a_credential_or_id(conf_on):
    leak = Resp({"error": {"code": 100, "message": f"Bad {TOKEN} for {WABA} and +919876543210"}}, 400)
    meta = FakeMeta(post=[leak, Resp({"id": TEMPLATE_ID, "status": "PENDING"})])
    out = StringIO()
    with patch("iic_booking.whatsapp.management.commands.whatsapp_sync_templates.TemplateClient", lambda: TemplateClient(session=meta)):
        with pytest.raises(CommandError):
            call_command("whatsapp_sync_templates", "--submit", "--batch-size", "2", "--delay", "0", stdout=out)
    stored = " ".join(str(a.detail) + a.meta_code for a in WhatsAppTemplateAudit.objects.all()) + " ".join(r.last_error for r in WhatsAppTemplate.objects.all())
    for text in (out.getvalue(), stored):
        for secret in (TOKEN, WABA, TEMPLATE_ID, "919876543210"):
            assert secret not in text


def test_the_commands_cannot_send_a_message():
    for module in ("templates", "template_client", "template_catalogue"):
        source = open(f"iic_booking/whatsapp/{module}.py", encoding="utf8").read()
        assert "send_template" not in source and "/messages" not in source and ".delete(" not in source


def test_templates_are_never_deleted_at_meta(conf_on):
    class NoDelete(FakeMeta):
        def delete(self, *args, **kwargs):
            raise AssertionError("deleted a template")

    run_command(NoDelete(rows=[meta_row(t, "REJECTED") for t in CATALOGUE]), "--status")


# --- status monitoring -----------------------------------------------------------------------------------------------


def test_status_sync_stores_everything_and_audits_changes(conf_on):
    t = BY_EVENT["payment_failed"]
    meta = FakeMeta(rows=[meta_row(t, "PENDING")])
    run_command(meta, "--status")
    record = WhatsAppTemplate.objects.get(name=t.name)
    assert record.status == "PENDING" and record.category == "UTILITY" and record.language == "en" and record.last_synced_at
    assert record.meta_id == TEMPLATE_ID and record.event == "payment_failed"
    meta.rows = [meta_row(t, "REJECTED", rejected_reason="INVALID_FORMAT")]
    out = run_command(meta, "--status")
    record.refresh_from_db()
    assert record.status == "REJECTED" and record.rejected_reason == "INVALID_FORMAT"
    change = WhatsAppTemplateAudit.objects.filter(action="status_changed", template_name=t.name).first()
    assert change.detail["from"] == "PENDING" and change.detail["to"] == "REJECTED"
    assert "reason=INVALID_FORMAT" in out and "variables" in out


@pytest.mark.parametrize("status", ["APPROVED", "PENDING", "REJECTED", "PAUSED", "DISABLED"])
def test_every_status_is_stored(conf_on, status):
    t = BY_EVENT["booking_reminder"]
    run_command(FakeMeta(rows=[meta_row(t, status)]), "--status")
    assert WhatsAppTemplate.objects.get(name=t.name).status == status


def test_an_unknown_status_never_counts_as_approved(conf_on):
    t = BY_EVENT["booking_reminder"]
    run_command(FakeMeta(rows=[meta_row(t, "SOMETHING_NEW")]), "--status")
    assert WhatsAppTemplate.objects.get(name=t.name).status == "UNKNOWN" and not templates.is_approved(t.name, t.language)


def test_the_report_groups_every_template(conf_on):
    rows = [meta_row(BY_EVENT["booking_confirmed"], "APPROVED"), meta_row(BY_EVENT["booking_cancelled"], "PENDING"),
            meta_row(BY_EVENT["booking_completed"], "REJECTED", rejected_reason="PROMOTIONAL"),
            meta_row(BY_EVENT["booking_reminder"], "PAUSED"), meta_row(BY_EVENT["waitlist_slot_available"], "DISABLED")]
    plan = templates.sync_statuses(TemplateClient(session=FakeMeta(rows=rows)))
    report = templates.build_report(meta_rows=plan.meta_rows)
    assert [i["name"] for i in report["approved"]] == ["iic_booking_confirmed"]
    assert [i["name"] for i in report["pending"]] == ["iic_booking_cancelled"]
    assert report["rejected"][0]["name"] == "iic_booking_completed" and "offer" in report["rejected"][0]["suggestion"]
    assert {i["name"] for i in report["paused_or_disabled"]} == {"iitr_booking_reminder", "iitr_waitlist_slot_available"}
    assert len(report["missing"]) == 8
    assert {i["name"] for i in report["requires_correction"]} == {"iic_booking_completed", "iitr_booking_reminder", "iitr_waitlist_slot_available"}
    assert report["not_wired"] == []
    assert report["last_synced_at"]


def test_wording_or_category_drift_is_reported_not_changed(conf_on):
    row = meta_row(BY_EVENT["booking_confirmed"], "APPROVED")
    row["components"][1]["text"] = "Different wording"
    other = meta_row(BY_EVENT["booking_cancelled"], "APPROVED", category="MARKETING")
    meta = FakeMeta(rows=[row, other])
    out = run_command(meta, "--status")
    assert "iic_booking_confirmed: wording at Meta differs" in out and "iic_booking_cancelled: Meta categorised it MARKETING" in out
    assert meta.posts == []


def test_the_report_command_works_without_meta(conf_on):
    out = StringIO()
    call_command("whatsapp_template_report", stdout=out)
    assert "Missing: 13" in out.getvalue() and "Last synchronised: never" in out.getvalue()


# --- controlled corrections ------------------------------------------------------------------------------------------


def rejected_record(event="payment_failed", **fields):
    t = BY_EVENT[event]
    return t, WhatsAppTemplate.objects.create(waba_id=WABA, name=t.name, language=t.language, status="REJECTED", meta_id=TEMPLATE_ID, **fields)


def test_a_rejected_template_is_not_corrected_without_new_wording(conf_on):
    t, _ = rejected_record(wording_hash=BY_EVENT["payment_failed"].wording_hash())
    meta = FakeMeta(rows=[meta_row(t, "REJECTED")])
    with pytest.raises(CommandError, match="has not changed"):
        run_command(meta, "--correct", t.name)
    assert meta.posts == [] and WhatsAppTemplateAudit.objects.filter(action="correction_refused").exists()


def test_a_correction_edits_the_existing_template_once(conf_on):
    t, record = rejected_record(wording_hash="older-wording")
    meta = FakeMeta(rows=[meta_row(t, "REJECTED")], post=[Resp({"success": True})])
    out = run_command(meta, "--correct", t.name)
    assert "CORRECTED" in out and len(meta.posts) == 1
    url, body = meta.posts[0]
    assert url.endswith(f"/{TEMPLATE_ID}") and set(body) == {"components"}
    record.refresh_from_db()
    assert record.status == "PENDING" and record.correction_attempts == 1 and record.wording_hash == t.wording_hash()


def test_corrections_stop_at_the_limit(conf_on):
    t, _ = rejected_record(wording_hash="older-wording", correction_attempts=templates.MAX_CORRECTIONS)
    meta = FakeMeta(rows=[meta_row(t, "REJECTED")])
    with pytest.raises(CommandError, match="already been corrected"):
        run_command(meta, "--correct", t.name)
    assert meta.posts == []


def test_only_rejected_templates_can_be_corrected(conf_on):
    t = BY_EVENT["payment_failed"]
    meta = FakeMeta(rows=[meta_row(t, "APPROVED")])
    with pytest.raises(CommandError, match="not rejected"):
        run_command(meta, "--correct", t.name)
    assert meta.posts == []


def test_status_or_submit_never_corrects_on_its_own(conf_on):
    t, _ = rejected_record(wording_hash="older-wording")
    meta = FakeMeta(rows=[meta_row(x, "REJECTED") for x in CATALOGUE])
    run_command(meta, "--status")
    run_command(meta, "--submit", "--delay", "0")
    assert meta.posts == []


# --- the send gate ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED", "UNKNOWN", "NOT_SUBMITTED"])
def test_nothing_is_queued_unless_approved(sending, status):
    t = BY_EVENT["payment_failed"]
    WhatsAppTemplate.objects.create(waba_id=WABA, name=t.name, language=t.language, status=status)
    with patch("iic_booking.whatsapp.service._enqueue") as enqueue:
        msg = service.notify(opted_in(), "payment_failed", ["A", "B", "C", "D"], event_key="k")
    assert msg.status == WhatsAppMessage.Status.SKIPPED and "not approved" in msg.error
    enqueue.assert_not_called()


def test_a_template_with_no_row_is_not_approved(sending):
    msg = service.notify(opted_in(), "payment_failed", ["A", "B", "C", "D"], event_key="k")
    assert msg.status == WhatsAppMessage.Status.SKIPPED


def test_an_approved_row_for_another_account_does_not_count(sending):
    t = BY_EVENT["payment_failed"]
    WhatsAppTemplate.objects.create(waba_id="other", name=t.name, language=t.language, status="APPROVED")
    assert not templates.is_approved(t.name, t.language)


def test_with_no_waba_configured_nothing_is_approved(sending):
    approve("payment_failed")
    sending.WHATSAPP_WABA_ID = ""
    assert not templates.is_approved(BY_EVENT["payment_failed"].name, "en")


def test_an_approved_template_is_queued_once(sending, django_capture_on_commit_callbacks):
    approve("payment_failed")
    user = opted_in()
    with django_capture_on_commit_callbacks(), patch("iic_booking.whatsapp.service._enqueue"):
        first = service.notify(user, "payment_failed", ["A", "B", "C", "D"], event_key="same")
        again = service.notify(user, "payment_failed", ["A", "B", "C", "D"], event_key="same")
    assert first.status == WhatsAppMessage.Status.QUEUED and again is None and WhatsAppMessage.objects.count() == 1


def test_a_template_paused_after_queueing_is_not_sent(sending):
    approve("payment_failed")
    user = opted_in()
    msg = WhatsAppMessage.objects.create(user=user, to_e164="+919876543210", event="payment_failed", template=BY_EVENT["payment_failed"].name,
                                         params=["A", "B", "C", "D"], event_key="k")
    WhatsAppTemplate.objects.update(status="PAUSED")
    with patch("iic_booking.whatsapp.client.send_template") as send:
        assert service.deliver(msg.pk) == WhatsAppMessage.Status.SKIPPED
    send.assert_not_called()


def test_the_wrong_number_of_values_sends_nothing(sending):
    approve("booking_confirmed")
    assert service.notify(opted_in(), "booking_confirmed", ["only", "three", "values"], event_key="k") is None
    assert not WhatsAppMessage.objects.exists()


def test_opt_in_is_still_required(sending):
    approve("payment_failed")
    user = UserFactory()
    assert service.notify(user, "payment_failed", ["A", "B", "C", "D"], event_key="k") is None
    WhatsAppOptIn.objects.create(user=user, phone_e164="+919876543210", opted_in=False)
    assert service.notify(user, "payment_failed", ["A", "B", "C", "D"], event_key="k") is None
    assert not WhatsAppMessage.objects.exists()


def test_the_pilot_list_still_applies(sending):
    approve("payment_failed")
    sending.WHATSAPP_PILOT_NUMBERS = ["+919800000000"]
    assert service.notify(opted_in(), "payment_failed", ["A", "B", "C", "D"], event_key="k") is None


def test_sending_off_means_nothing_even_when_approved(conf_on):
    conf_on.WHATSAPP_ENABLED = False
    approve()
    assert service.notify(opted_in(), "payment_failed", ["A", "B", "C", "D"], event_key="k") is None


def test_an_unknown_event_sends_nothing(sending):
    assert service.notify(opted_in(), "something_else", ["A"], event_key="k") is None


# --- event to template mapping ---------------------------------------------------------------------------------------


def fake_booking(status="BOOKED"):
    user = UserFactory(user_type=UserType.STUDENT, name="Asha Verma")
    return SimpleNamespace(pk=7, booking_id=7, virtual_booking_id="IIC-7", user=user, status=status, amount_due=1500, completed_at=None,
                           equipment=SimpleNamespace(name="XRD"), fabrication_rejected_at=timezone.now())


def fake_event(booking, kind, *, previous="", new=""):
    return SimpleNamespace(pk=1, event_id=99, booking=booking, event_type=kind, previous_status=previous, new_status=new)


@pytest.mark.parametrize("status, kind, previous, new, expected", [
    ("BOOKED", "CREATED", "", "", "booking_confirmed"),
    ("BOOKED", "CONFIRMED", "", "", "booking_confirmed"),
    ("BOOKED", "STATUS_CHANGED", "PENDING", "BOOKED", "booking_confirmed"),
    ("BOOKED", "STATUS_CHANGED", "PENDING_PAYMENT", "BOOKED", "booking_confirmed"),
    ("BOOKED", "STATUS_CHANGED", "HOLD", "BOOKED", "booking_confirmed"),
    ("CANCELLED", "CANCELLED", "", "", "booking_cancelled"),
    ("REFUNDED", "REFUNDED", "", "", "booking_cancelled"),
    ("BOOKED", "RESCHEDULED", "", "", "booking_rescheduled"),
    ("COMPLETED", "COMPLETED", "", "", "booking_completed"),
])
def test_each_booking_event_maps_to_its_template_with_the_right_values(sending, status, kind, previous, new, expected):
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.booking_event(fake_event(fake_booking(status), kind, previous=previous, new=new))
    args = notify.call_args
    assert args.args[1] == expected and len(args.args[2]) == len(BY_EVENT[expected].params)
    # One confirmation per booking (shared with the payment path); every other event once per event.
    assert args.kwargs["event_key"].endswith("booking_confirmed:payment") if expected == "booking_confirmed" else "99" in args.kwargs["event_key"]


@pytest.mark.parametrize("status, kind, previous, new", [
    ("BOOKED", "COMMENT", "", ""),
    ("BOOKED", "STATUS_CHANGED", "BOOKING_NOT_UTILIZED", "BOOKED"),
    ("HOLD", "STATUS_CHANGED", "BOOKED", "HOLD"),
    ("WAITLISTED", "CREATED", "", ""),
    ("PENDING", "CREATED", "", ""),
    ("PENDING_PAYMENT", "CREATED", "", ""),
])
def test_other_booking_events_send_nothing(sending, status, kind, previous, new):
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.booking_event(fake_event(fake_booking(status), kind, previous=previous, new=new))
    notify.assert_not_called()


def test_every_hook_does_nothing_while_whatsapp_is_off(conf_on):
    conf_on.WHATSAPP_ENABLED = False
    booking = fake_booking()
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.booking_event(fake_event(booking, "CREATED"))
        hooks.booking_rejected(booking)
        hooks.booking_reminder(booking)
        hooks.waitlist_slot_available(SimpleNamespace(pk=1, user=booking.user), booking.equipment, timezone.now())
        hooks.payment_order_saved(SimpleNamespace(booking_id=None), True)
        hooks.payment_refund_saved(SimpleNamespace(), True)
        hooks.external_refund_saved(SimpleNamespace(), True)
    notify.assert_not_called()


def test_a_hook_problem_never_reaches_the_caller(sending):
    with patch("iic_booking.whatsapp.service.notify", side_effect=RuntimeError("boom")):
        hooks.booking_event(fake_event(fake_booking(), "CREATED"))


def test_a_payment_confirmation_uses_the_confirmed_template_once(sending):
    approve("booking_confirmed")
    booking = fake_booking()
    booking.user = opted_in()
    booking.pk = 1
    with patch("iic_booking.whatsapp.service._enqueue"):
        hooks.booking_confirmed_by_payment(booking)
        hooks.booking_confirmed_by_payment(booking)
    msg = WhatsAppMessage.objects.get()
    assert msg.event == "booking_confirmed" and len(msg.params) == 5 and msg.status == WhatsAppMessage.Status.QUEUED


def test_a_payment_confirmation_does_nothing_while_whatsapp_is_off(conf_on):
    conf_on.WHATSAPP_ENABLED = False
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.booking_confirmed_by_payment(fake_booking())
    notify.assert_not_called()


def test_every_awaiting_payment_path_tells_the_user():
    for path in ("payments/razorpay_service.py", "users/api/payment_views.py"):
        assert "booking_confirmed_by_payment(booking)" in open(f"iic_booking/{path}", encoding="utf8").read(), path


def test_rejection_reminder_and_waitlist_map_correctly(sending):
    booking = fake_booking()
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.booking_rejected(booking)
        hooks.booking_reminder(booking)
        hooks.waitlist_slot_available(SimpleNamespace(pk=3, user=booking.user), booking.equipment, timezone.now())
    sent = [(c.args[1], len(c.args[2])) for c in notify.call_args_list]
    assert sent == [("booking_rejected", 4), ("booking_reminder", 5), ("waitlist_slot_available", 4)]


@pytest.mark.parametrize("created, status, earlier_paid, expected", [
    (True, "CREATED", False, None),
    # The payment that confirms the booking: Booking Confirmation already says so, no second message.
    (False, "PAID", False, None),
    # A later payment on the same booking (e.g. an extra charge) is still announced.
    (False, "PAID", True, "payment_successful"),
    (False, "FAILED", False, "payment_failed"),
    (False, "CREATED", False, None), (False, "EXPIRED", False, None),
])
def test_payment_order_mapping(sending, created, status, earlier_paid, expected):
    booking = fake_booking()
    order = SimpleNamespace(pk=5, booking_id=7, booking=booking, user=booking.user, status=status, total_amount=1500)
    with patch("iic_booking.whatsapp.service.notify") as notify,             patch("iic_booking.whatsapp.hooks._earlier_paid_order", return_value=earlier_paid):
        hooks.payment_order_saved(order, created)
    if expected is None:
        notify.assert_not_called()
    else:
        assert notify.call_args.args[1] == expected and notify.call_args.args[2][3] == "Rs. 1,500.00"


def test_a_wallet_recharge_order_sends_nothing(sending):
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.payment_order_saved(SimpleNamespace(pk=5, booking_id=None, status="PAID"), False)
    notify.assert_not_called()


@pytest.mark.parametrize("created, status, expected", [
    (True, "queued", None), (True, "closed", "refund_completed"), (False, "closed", "refund_completed"), (False, "sent", None),
])
def test_external_refund_mapping(sending, created, status, expected):
    booking = fake_booking()
    refund = SimpleNamespace(pk=4, user=booking.user, booking=booking, status=status, refund_amount=200)
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.external_refund_saved(refund, created)
    if expected is None:
        notify.assert_not_called()
    else:
        assert notify.call_args.args[1] == expected and len(notify.call_args.args[2]) == 3


@pytest.mark.parametrize("created, status, expected", [
    (True, "PENDING", None), (False, "PROCESSED", "refund_completed"), (False, "PENDING", None), (False, "FAILED", None),
])
def test_gateway_refund_mapping(sending, created, status, expected):
    booking = fake_booking()
    payment = SimpleNamespace(payment_order=SimpleNamespace(booking_id=7, booking=booking, user=booking.user))
    refund = SimpleNamespace(pk=4, payment=payment, status=status, amount=200)
    with patch("iic_booking.whatsapp.service.notify") as notify:
        hooks.payment_refund_saved(refund, created)
    if expected is None:
        notify.assert_not_called()
    else:
        assert notify.call_args.args[1] == expected


def test_the_signals_are_connected_to_real_models():
    from django.db.models.signals import post_save

    from iic_booking.external_services.models import ExternalRefund
    from iic_booking.payments.models import PaymentOrder, PaymentRefund

    for model, hook in ((PaymentOrder, "payment_order_saved"), (PaymentRefund, "payment_refund_saved"),
                        (ExternalRefund, "external_refund_saved")):
        instance = model()
        with patch(f"iic_booking.whatsapp.hooks.{hook}") as called:
            post_save.send(sender=model, instance=instance, created=True, raw=False)
            called.assert_called_once_with(instance, True)
            called.reset_mock()
            post_save.send(sender=model, instance=instance, created=True, raw=True)  # fixtures being loaded
            called.assert_not_called()


def test_every_wired_catalogue_event_has_a_code_path():
    hook_source = open("iic_booking/whatsapp/hooks.py", encoding="utf8").read() + open("iic_booking/whatsapp/otp.py", encoding="utf8").read()
    for template in CATALOGUE:
        if template.trigger:
            assert f'"{template.event}"' in hook_source, template.event
        else:
            assert f'"{template.event}"' not in hook_source, template.event


def test_the_daily_task_does_nothing_without_a_waba(settings):
    from iic_booking.whatsapp.tasks import sync_template_statuses

    settings.WHATSAPP_WABA_ID = ""
    with patch("iic_booking.whatsapp.template_client.TemplateClient") as client:
        assert sync_template_statuses() == {"skipped": "no WHATSAPP_WABA_ID"}
    client.assert_not_called()


def test_the_lifecycle_call_sites_exist():
    for path, name in (("equipment/booking_reminders.py", "booking_reminder"), ("equipment/waitlist.py", "waitlist_slot_available"),
                       ("equipment/fabrication_workflow.py", "booking_rejected")):
        assert f"from iic_booking.whatsapp.hooks import {name}" in open(f"iic_booking/{path}", encoding="utf8").read()


def test_a_whole_booking_journey_sends_one_message_per_step(sending):
    """Confirmed (once, whichever path reports it), payment, rescheduled, completed: in that order, nothing twice."""
    for event in ("booking_confirmed", "booking_rescheduled", "booking_completed", "booking_cancelled"):
        approve(event)
    booking = fake_booking("BOOKED")
    booking.user = opted_in()
    booking.pk = 11
    with patch("iic_booking.whatsapp.service._enqueue"), patch("iic_booking.whatsapp.hooks._first_slot", lambda b: None):
        hooks.booking_event(SimpleNamespace(pk=1, event_id=1, booking=booking, event_type="CREATED", previous_status="", new_status="BOOKED"))
        hooks.booking_confirmed_by_payment(booking)  # the payment path reports the same confirmation
        hooks.booking_event(SimpleNamespace(pk=2, event_id=2, booking=booking, event_type="STATUS_CHANGED",
                                            previous_status="PENDING_PAYMENT", new_status="BOOKED"))
        hooks.booking_event(SimpleNamespace(pk=3, event_id=3, booking=booking, event_type="RESCHEDULED", previous_status="", new_status=""))
        booking.status = "COMPLETED"
        hooks.booking_event(SimpleNamespace(pk=4, event_id=4, booking=booking, event_type="COMPLETED", previous_status="BOOKED", new_status="COMPLETED"))
        hooks.booking_event(SimpleNamespace(pk=5, event_id=5, booking=booking, event_type="COMMENT", previous_status="", new_status=""))
    events = list(WhatsAppMessage.objects.order_by("id").values_list("event", flat=True))
    assert events == ["booking_confirmed", "booking_rescheduled", "booking_completed"]
