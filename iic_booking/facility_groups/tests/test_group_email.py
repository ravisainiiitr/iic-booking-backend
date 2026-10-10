import json
import uuid

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.facility_groups import group_email
from iic_booking.facility_groups.models import (
    CampaignStatus,
    FacilityUserGroup,
    GroupEmailCampaign,
    RecipientStatus,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_booking, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def booked(world, run_on_commit):
    run_on_commit(make_booking, world.student, world.fesem)
    run_on_commit(make_booking, world.external, world.tem)
    world.em_group = FacilityUserGroup.objects.get(auto_key=f"category:{world.em.pk}")
    world.xrd_group = None
    return world


def payload(world, **extra):
    data = {
        "group_ids": [world.em_group.pk],
        "filters": {},
        "subject": "EM facility maintenance",
        "body_html": "<p>Dear {{ name }},</p><p>The <span style=\"color: var(--rt-red)\">FE-SEM</span> is down.</p>",
        "idempotency_key": uuid.uuid4().hex,
    }
    data.update(extra)
    return data


def send(world, **extra):
    res = client_for(world.admin).post(f"{API}/email/send/", payload(world, **extra), format="json")
    assert res.status_code in (200, 201), res.data
    return GroupEmailCampaign.objects.get(pk=res.data["campaign"]["id"])


def test_preview_counts_and_departments(booked):
    res = client_for(booked.admin).post(
        f"{API}/email/preview/", {"group_ids": [booked.em_group.pk], "filters": {}}, format="json"
    )
    assert res.status_code == 200
    assert (res.data["total"], res.data["internal"], res.data["external"]) == (2, 1, 1)
    assert {r["email"] for r in res.data["recipients"]} == {booked.student.email.lower(), booked.external.email.lower()}
    res = client_for(booked.admin).post(
        f"{API}/email/preview/",
        {"group_ids": [booked.em_group.pk], "filters": {"include_supervisors": True, "audience": "internal"}},
        format="json",
    )
    assert res.data["total"] == 2  # student + supervising faculty


def test_send_individually_with_single_cc_bcc_summary(booked):
    campaign = send(booked, cc="hod@iitr.ac.in", bcc=["records@iitr.ac.in"])
    assert campaign.status == CampaignStatus.QUEUED and campaign.total_recipients == 2
    assert mail.outbox == []  # queued for Celery; nothing sent in the request

    group_email.process_campaign(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENT and campaign.sent_count == 2

    individual = [m for m in mail.outbox if not m.cc and not m.bcc]
    summary = [m for m in mail.outbox if m.cc or m.bcc or "hod@iitr.ac.in" in m.to]
    assert len(individual) == 2 and len(summary) == 1
    assert sorted(m.to[0] for m in individual) == sorted([booked.student.email.lower(), booked.external.email.lower()])
    assert all(len(m.to) == 1 for m in individual)
    assert summary[0].to == ["hod@iitr.ac.in"] and summary[0].bcc == ["records@iitr.ac.in"]
    assert "sent individually to 2 recipients" in summary[0].body

    student_mail = next(m for m in individual if m.to[0] == booked.student.email.lower())
    html = student_mail.alternatives[0][0]
    assert "Dear Student One" in html
    assert "var(--rt-red)" not in html and "#" in html
    assert "Indian Institute of Technology Roorkee" in html


def test_cc_on_each_email_mode(booked):
    campaign = send(booked, cc="hod@iitr.ac.in", bcc="records@iitr.ac.in", cc_mode="each")
    group_email.process_campaign(campaign.pk)
    assert len(mail.outbox) == 2
    assert all(m.cc == ["hod@iitr.ac.in"] and m.bcc == ["records@iitr.ac.in"] for m in mail.outbox)


def test_each_mode_limited_for_large_lists(booked, monkeypatch):
    monkeypatch.setattr(group_email, "EACH_MODE_MAX_RECIPIENTS", 1)
    res = client_for(booked.admin).post(
        f"{API}/email/send/", payload(booked, cc="hod@iitr.ac.in", cc_mode="each"), format="json"
    )
    assert res.status_code == 400 and res.data["field"] == "cc_mode"


def test_no_double_send_on_retry_or_resubmit(booked):
    data = payload(booked)
    first = client_for(booked.admin).post(f"{API}/email/send/", data, format="json")
    again = client_for(booked.admin).post(f"{API}/email/send/", data, format="json")
    assert first.status_code == 201 and again.status_code == 200
    assert first.data["campaign"]["id"] == again.data["campaign"]["id"]
    assert GroupEmailCampaign.objects.count() == 1

    campaign_id = first.data["campaign"]["id"]
    group_email.process_campaign(campaign_id)
    group_email.process_campaign(campaign_id)  # duplicated task delivery
    assert len(mail.outbox) == 2


def test_batches_until_done(booked, settings):
    settings.FACILITY_GROUP_EMAIL_BATCH_SIZE = 1
    campaign = send(booked)
    assert group_email.process_campaign(campaign.pk)["remaining"] is True
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENDING and campaign.sent_count == 1
    assert group_email.process_campaign(campaign.pk)["remaining"] is False
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENT and len(mail.outbox) == 2


def test_failed_recipient_retry_sends_only_failures(booked, monkeypatch):
    campaign = send(booked)
    real_send = group_email.EmailMultiAlternatives.send

    def flaky(self, *args, **kwargs):
        if self.to == [booked.external.email.lower()]:
            raise OSError("SMTP refused")
        return real_send(self, *args, **kwargs)

    monkeypatch.setattr(group_email.EmailMultiAlternatives, "send", flaky)
    group_email.process_campaign(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.PARTIAL and (campaign.sent_count, campaign.failed_count) == (1, 1)
    failed = campaign.recipients.get(status=RecipientStatus.FAILED)
    assert "SMTP refused" in failed.error

    monkeypatch.setattr(group_email.EmailMultiAlternatives, "send", real_send)
    mail.outbox.clear()
    res = client_for(booked.admin).post(f"{API}/email/campaigns/{campaign.pk}/resume/", {"retry_failed": True}, format="json")
    assert res.status_code == 200
    group_email.process_campaign(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.SENT
    assert [m.to for m in mail.outbox] == [[booked.external.email.lower()]]


def test_send_test_to_self_only(booked):
    res = client_for(booked.admin).post(f"{API}/email/test/", payload(booked, cc="hod@iitr.ac.in"), format="json")
    assert res.status_code == 200
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == [booked.admin.email] and not msg.cc
    assert msg.subject.startswith("[TEST]")
    assert not GroupEmailCampaign.objects.exists()


def test_test_email_with_attachment_multipart(booked):
    upload = SimpleUploadedFile("notice.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    res = client_for(booked.admin).post(
        f"{API}/email/test/", {"payload": json.dumps(payload(booked)), "attachments": [upload]}, format="multipart"
    )
    assert res.status_code == 200, res.data
    assert mail.outbox[0].attachments[0][0] == "notice.pdf"


def test_campaign_attachment_sent_to_every_recipient(booked, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    upload = SimpleUploadedFile("schedule.txt", b"Mon-Fri", content_type="text/plain")
    res = client_for(booked.admin).post(
        f"{API}/email/send/", {"payload": json.dumps(payload(booked)), "attachments": [upload]}, format="multipart"
    )
    assert res.status_code == 201, res.data
    group_email.process_campaign(res.data["campaign"]["id"])
    assert len(mail.outbox) == 2
    assert all(m.attachments[0][0] == "schedule.txt" for m in mail.outbox)


def test_blocked_attachment_type(booked):
    upload = SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream")
    res = client_for(booked.admin).post(
        f"{API}/email/test/", {"payload": json.dumps(payload(booked)), "attachments": [upload]}, format="multipart"
    )
    assert res.status_code == 400 and res.data["field"] == "attachments"


def test_validation(booked):
    admin = client_for(booked.admin)
    assert admin.post(f"{API}/email/send/", payload(booked, subject=""), format="json").data["field"] == "subject"
    assert admin.post(f"{API}/email/send/", payload(booked, cc="not-an-email"), format="json").data["field"] == "cc"
    assert admin.post(f"{API}/email/send/", payload(booked, group_ids=[]), format="json").data["field"] == "group_ids"
    res = admin.post(f"{API}/email/send/", payload(booked, expected_recipients=5), format="json")
    assert res.status_code == 409 and res.data["total"] == 2
    empty = admin.post(
        f"{API}/email/send/", payload(booked, filters={"user_types": [UserType.RND]}), format="json"
    )
    assert empty.status_code == 400


def test_test_accounts_excluded_from_recipients(booked, run_on_commit):
    tester = make_user(user_type=UserType.STUDENT, department=booked.chem, is_test_account=True)
    run_on_commit(make_booking, tester, booked.fesem)
    campaign = send(booked)
    assert campaign.total_recipients == 2


def test_cancel_stops_pending(booked, settings):
    settings.FACILITY_GROUP_EMAIL_BATCH_SIZE = 1
    campaign = send(booked)
    group_email.process_campaign(campaign.pk)
    res = client_for(booked.admin).post(f"{API}/email/campaigns/{campaign.pk}/cancel/", {}, format="json")
    assert res.data["status"] == CampaignStatus.CANCELLED
    group_email.process_campaign(campaign.pk)
    assert len(mail.outbox) == 1


def test_campaign_history_and_detail(booked):
    campaign = send(booked, cc="hod@iitr.ac.in")
    group_email.process_campaign(campaign.pk)
    admin = client_for(booked.admin)
    rows = admin.get(f"{API}/email/campaigns/").data["results"]
    assert rows[0]["subject"] == "EM facility maintenance" and rows[0]["sent_count"] == 2
    assert rows[0]["group_names"] == ["Electron Microscopy"] and rows[0]["created_by"]
    detail = admin.get(f"{API}/email/campaigns/{campaign.pk}/").data
    assert detail["status_counts"]["sent"] == 2 and detail["summary_sent_at"]
    assert len(detail["recipients"]["results"]) == 2


def test_render_preview(booked):
    res = client_for(booked.admin).post(
        f"{API}/email/render/", {"subject": "Hello", "body_html": "<p>Hi {{name}}</p>"}, format="json"
    )
    assert res.status_code == 200 and "Hi Recipient Name" in res.data["html"]
