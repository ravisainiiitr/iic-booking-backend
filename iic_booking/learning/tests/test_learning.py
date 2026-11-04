"""Online learning: content by equipment staff, sign-off and quiz completion, the training-request prerequisite,
answers never leaking before submission, attempt limits, PDF checks, and the booking logbook."""

from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.learning import service
from iic_booking.learning.models import LearningModule, LearningSetting, LogbookEntry, QuizQuestion
from iic_booking.training.tests.conftest import API as TRAINING_API
from iic_booking.training.tests.conftest import client_for, seed_levels, training_settings, world  # noqa: F401
from iic_booking.training.tests.test_authorisation_gate import _no_portal_lock, gate_on, make_booking  # noqa: F401

pytestmark = pytest.mark.django_db
API = "/api/v1/learning"


@pytest.fixture(autouse=True)
def _quiet():
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as m, mock.patch("iic_booking.training.notify.send"):
        yield m


def module_with_quiz(world, **kw) -> LearningModule:
    m = LearningModule.objects.create(equipment=world.equipment, title="XRD safety and SOP", body="Radiation safety...",
                                      pass_mark_pct=kw.pop("pass_mark_pct", 50), **kw)
    QuizQuestion.objects.create(module=m, text="Shutter before opening the door?", options=["Open", "Closed"], correct=[1], order=1)
    QuizQuestion.objects.create(module=m, text="Which are PPE?", options=["Lab coat", "Sandals", "Badge"], correct=[0, 2], order=2)
    m.is_published = True
    m.save()
    return m


def answers(m, first, second):
    q1, q2 = list(m.questions.order_by("order"))
    return {str(q1.pk): first, str(q2.pk): second}


# --------------------------------------------------------------------------- content
def test_staff_create_publish_and_learners_never_see_answers(world):
    url = f"{API}/equipment/{world.equipment.pk}/modules/"
    assert client_for(world.student).post(url, {"title": "x"}, format="json").status_code == 403
    assert client_for(world.other_oic).post(url, {"title": "x"}, format="json").status_code == 403
    resp = client_for(world.operator).post(url, {"title": "Basics", "video_url": "http://insecure"}, format="json")
    assert resp.status_code == 400
    resp = client_for(world.operator).post(url, {"title": "Basics", "video_url": "https://video.example/xrd", "pass_mark_pct": 80}, format="json")
    assert resp.status_code == 201 and resp.data["is_published"] is False
    mid = resp.data["id"]
    assert client_for(world.student).get(url).data["results"] == []  # drafts are hidden
    # Publishing a quiz module needs a question.
    assert client_for(world.oic).patch(f"{API}/modules/{mid}/", {"is_published": True}, format="json").data["code"] == "no_questions"
    qurl = f"{API}/modules/{mid}/questions/"
    assert client_for(world.oic).post(qurl, {"text": "Q", "options": ["a"], "correct": [0]}, format="json").status_code == 400
    assert client_for(world.oic).post(qurl, {"text": "Q", "options": ["a", "b"], "correct": [2]}, format="json").status_code == 400
    q = client_for(world.oic).post(qurl, {"text": "Q", "options": ["a", "b"], "correct": [1], "explanation": "b"}, format="json")
    assert q.status_code == 201 and q.data["correct"] == [1]
    assert client_for(world.oic).patch(f"{API}/modules/{mid}/", {"is_published": True}, format="json").status_code == 200
    detail = client_for(world.student).get(f"{API}/modules/{mid}/").data
    assert detail["questions"] and "correct" not in detail["questions"][0] and "explanation" not in detail["questions"][0]
    # The last active question of a published quiz cannot be switched off.
    assert client_for(world.oic).patch(f"{API}/questions/{q.data['id']}/", {"is_active": False}, format="json").status_code == 400
    assert QuizQuestion.objects.get(pk=q.data["id"]).is_active is True


def test_document_must_be_a_real_small_pdf(world):
    m = module_with_quiz(world)
    url = f"{API}/modules/{m.pk}/document/"
    fake = SimpleUploadedFile("sop.pdf", b"not a pdf", content_type="application/pdf")
    assert client_for(world.operator).post(url, {"file": fake}, format="multipart").status_code == 400
    exe = SimpleUploadedFile("sop.exe", b"%PDF-1.4", content_type="application/octet-stream")
    assert client_for(world.operator).post(url, {"file": exe}, format="multipart").status_code == 400
    pdf = SimpleUploadedFile("sop.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf")
    assert client_for(world.student).post(url, {"file": pdf}, format="multipart").status_code == 403
    pdf.seek(0)
    resp = client_for(world.operator).post(url, {"file": pdf}, format="multipart")
    assert resp.status_code == 200 and resp.data["document_url"]


# --------------------------------------------------------------------------- completion and quiz
def test_quiz_scoring_explanations_after_submit_and_completion(world):
    m = module_with_quiz(world)
    url = f"{API}/modules/{m.pk}/attempt/"
    first = client_for(world.student).post(url, {"answers": answers(m, [0], [0])}, format="json").data
    assert first["score_pct"] == 0 and not first["passed"] and first["results"][0]["correct_options"] == [1]
    second = client_for(world.student).post(url, {"answers": answers(m, [1], [2, 0])}, format="json").data
    assert second["score_pct"] == 100 and second["passed"] and second["complete"] is False  # sign-off still due
    ack = client_for(world.student).post(f"{API}/modules/{m.pk}/acknowledge/", {}, format="json").data
    assert ack["complete"] is True
    listing = client_for(world.student).get(f"{API}/equipment/{world.equipment.pk}/modules/").data
    assert listing["required_incomplete"] == [] and listing["results"][0]["progress"]["best_score_pct"] == 100
    # Staff see who is ready.
    prog = client_for(world.oic).get(f"{API}/equipment/{world.equipment.pk}/progress/").data
    assert prog["results"][0]["user"]["id"] == world.student.pk and prog["results"][0]["required_complete"]
    assert client_for(world.student).get(f"{API}/equipment/{world.equipment.pk}/progress/").status_code == 403


def test_attempts_are_limited_per_day(world):
    m = module_with_quiz(world, max_attempts_per_day=2)
    url = f"{API}/modules/{m.pk}/attempt/"
    for _ in range(2):
        assert client_for(world.student).post(url, {"answers": {}}, format="json").status_code == 200
    resp = client_for(world.student).post(url, {"answers": {}}, format="json")
    assert resp.status_code == 429 and resp.data["code"] == "too_many_attempts"


def test_training_request_waits_for_required_modules_when_switched_on(world):
    gate_on(world.equipment)
    m = module_with_quiz(world)
    req_url = f"{TRAINING_API}/authorisation/requests/"
    setting_url = f"{API}/equipment/{world.equipment.pk}/settings/"
    assert client_for(world.operator).put(setting_url, {"modules_before_training_request": True}, format="json").status_code == 403
    assert client_for(world.oic).put(setting_url, {"modules_before_training_request": True}, format="json").status_code == 200
    resp = client_for(world.student).post(req_url, {"equipment_id": world.equipment.pk}, format="json")
    assert resp.status_code == 400 and resp.data["code"] == "modules_required"
    assert service.training_request_blocker(world.student, world.equipment) == [m.title]
    client_for(world.student).post(f"{API}/modules/{m.pk}/attempt/", {"answers": answers(m, [1], [0, 2])}, format="json")
    client_for(world.student).post(f"{API}/modules/{m.pk}/acknowledge/", {}, format="json")
    assert client_for(world.student).post(req_url, {"equipment_id": world.equipment.pk}, format="json").status_code == 201
    LearningSetting.objects.filter(equipment=world.equipment).update(modules_before_training_request=False)
    assert service.training_request_blocker(world.student2, world.equipment) == []


# --------------------------------------------------------------------------- logbook
def test_logbook_for_booker_and_staff_and_issue_alerts_staff(world, _quiet, django_capture_on_commit_callbacks):
    b = make_booking(world.student, world.equipment)
    url = f"{API}/bookings/{b.pk}/logbook/"
    assert client_for(world.student2).get(url).status_code == 403
    assert client_for(world.student).post(url, {"text": ""}, format="json").status_code == 400
    assert client_for(world.student).post(url, {"text": "2θ 10-80°, step 0.02", "kind": "PARAMETERS"}, format="json").status_code == 201
    with django_capture_on_commit_callbacks(execute=True):
        resp = client_for(world.student).post(url, {"text": "Tube current dropped mid-run", "kind": "ISSUE"}, format="json")
    assert resp.status_code == 201
    _quiet.assert_called_once()
    recipients = {u.pk for u in _quiet.call_args[0][0]}
    assert world.oic.pk in recipients and world.operator.pk in recipients and world.student.pk not in recipients
    assert client_for(world.operator).post(url, {"text": "Reset tube; fine", "kind": "NOTE"}, format="json").status_code == 201
    rows = client_for(world.student).get(url).data["results"]
    assert [r["kind"] for r in rows] == ["PARAMETERS", "ISSUE", "NOTE"]
    eq_url = f"{API}/equipment/{world.equipment.pk}/logbook/"
    assert client_for(world.student).get(eq_url).status_code == 403
    assert [r["kind"] for r in client_for(world.oic).get(eq_url, {"kind": "ISSUE"}).data["results"]] == ["ISSUE"]
    assert LogbookEntry.objects.count() == 3
