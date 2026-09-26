"""Presigned upload/download flows against an in-memory S3 fake, file policy, and the cleanup task."""

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.my_research import file_policy, tasks
from iic_booking.my_research.models import ActivityAction, FileStatus, ResearchActivity, ResearchFile

from .conftest import API, make_client, sha256_b64, upload_file

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"1 0 obj\n" * 50
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 200
CSV = b"time,intensity\n0,1.2\n1,3.4\n"


@pytest.fixture(autouse=True)
def _no_connection_recycling(monkeypatch):
    monkeypatch.setattr(tasks, "close_old_connections", lambda: None)


def _initiate(client, ws, **data):
    return client.post(f"{API}/workspaces/{ws}/uploads/initiate/", data, format="json")


# ---------------------------------------------------------------- single PUT


def test_single_upload_happy_path(student, workspace, fake_s3):
    client = make_client(student)
    init = _initiate(client, workspace["id"], filename="XRD Report.pdf", size=len(PDF), content_type="application/pdf")
    assert init.status_code == 201, init.data
    upload = init.data["upload"]
    assert upload["mode"] == "single"
    assert upload["method"] == "PUT"
    assert upload["headers"]["Content-Type"] == "application/pdf"
    assert upload["url"].startswith("https://fake-s3.test/put_object/")
    file_id = init.data["file"]["id"]
    assert init.data["file"]["status"] == FileStatus.PENDING_UPLOAD

    row = ResearchFile.objects.get(pk=file_id)
    assert row.storage_key == f"research/workspaces/{workspace['id']}/files/{file_id}/XRD_Report.pdf"
    presigned = fake_s3.presigned[-1]
    assert presigned["params"]["Bucket"] == "test-research-bucket"
    assert presigned["expires"] == 3600

    fake_s3.put(row.storage_key, PDF)
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    assert done.status_code == 200, done.data
    assert done.data["status"] == FileStatus.AVAILABLE
    assert done.data["detected_type"] == "pdf"
    assert done.data["preview_kind"] == "pdf"
    row.refresh_from_db()
    assert row.etag == hashlib.md5(PDF).hexdigest()
    assert row.etag_is_md5 is True
    assert row.uploaded_at is not None
    assert ResearchActivity.objects.filter(action=ActivityAction.FILE_UPLOADED, target_id=file_id).exists()

    listing = client.get(f"{API}/workspaces/{workspace['id']}/files/").data["results"]
    assert [f["id"] for f in listing] == [file_id]


def test_pending_files_are_not_listed_or_downloadable(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    assert client.get(f"{API}/workspaces/{workspace['id']}/files/").data["results"] == []
    assert client.post(f"{API}/files/{file_id}/download/", {}, format="json").status_code == 404


def test_sha256_checksum_is_enforced_and_verified(student, workspace, fake_s3):
    client = make_client(student)
    digest = hashlib.sha256(PDF).hexdigest()
    file_id, done = upload_file(client, fake_s3, workspace["id"], "c.pdf", PDF, sha256=digest)
    assert done.status_code == 200
    headers_sent = fake_s3.presigned[-1]["params"]
    assert headers_sent["ChecksumSHA256"] == sha256_b64(PDF)
    assert done.data["checksum_verified"] is True


def test_checksum_mismatch_rejects_and_deletes_object(student, workspace, fake_s3):
    client = make_client(student)
    init = _initiate(client, workspace["id"], filename="c.pdf", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest())
    file_id = init.data["file"]["id"]
    key = ResearchFile.objects.get(pk=file_id).storage_key
    fake_s3.put(key, PDF, checksum_b64=sha256_b64(b"tampered"))
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    assert done.status_code == 422
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.FAILED
    assert key not in fake_s3.objects


def test_invalid_sha256_rejected(student, workspace, fake_s3):
    resp = _initiate(make_client(student), workspace["id"], filename="c.pdf", size=10, sha256="not-hex")
    assert resp.status_code == 400


def test_complete_before_object_exists_keeps_pending(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    assert done.status_code == 409
    assert done.data["code"] == "upload_not_found"
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.PENDING_UPLOAD


def test_size_mismatch_marks_failed_and_deletes_object(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF) + 10).data["file"]["id"]
    key = ResearchFile.objects.get(pk=file_id).storage_key
    fake_s3.put(key, PDF)
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    assert done.status_code == 422
    assert done.data["code"] == "upload_rejected"
    row = ResearchFile.objects.get(pk=file_id)
    assert row.status == FileStatus.FAILED
    assert "does not match" in row.failure_reason
    assert key not in fake_s3.objects


def test_temporary_storage_error_on_complete_keeps_file_pending(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    fake_s3.put(ResearchFile.objects.get(pk=file_id).storage_key, PDF)
    fake_s3.fail.add("head_object")
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    assert done.status_code == 503
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.PENDING_UPLOAD
    fake_s3.fail.clear()
    assert client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json").status_code == 200


def test_storage_failure_on_initiate_marks_failed(student, workspace, fake_s3):
    fake_s3.fail.add("presign")
    resp = _initiate(make_client(student), workspace["id"], filename="a.pdf", size=10)
    assert resp.status_code == 503
    assert ResearchFile.objects.get().status == FileStatus.FAILED


def test_storage_not_configured(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_S3_BUCKET = ""
    settings.AWS_STORAGE_BUCKET_NAME = ""
    resp = _initiate(make_client(student), workspace["id"], filename="a.pdf", size=10)
    assert resp.status_code == 503
    assert resp.data["code"] == "storage_unavailable"


def test_abort_marks_failed_and_removes_object(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    key = ResearchFile.objects.get(pk=file_id).storage_key
    fake_s3.put(key, PDF)
    assert client.post(f"{API}/uploads/{file_id}/abort/", {}, format="json").status_code == 200
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.FAILED
    assert key not in fake_s3.objects
    assert client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json").status_code == 404


def test_other_owner_cannot_complete_upload(student, other_student, workspace, fake_s3):
    file_id = _initiate(make_client(student), workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    assert make_client(other_student).post(f"{API}/uploads/{file_id}/complete/", {}, format="json").status_code == 404


# ---------------------------------------------------------------- multipart


def test_multipart_upload_flow(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MULTIPART_THRESHOLD = 100
    body = PDF + b"x" * 500
    client = make_client(student)
    init = _initiate(client, workspace["id"], filename="big-dataset.pdf", size=len(body))
    assert init.status_code == 201
    assert init.data["upload"]["mode"] == "multipart"
    assert init.data["upload"]["part_size"] >= 5 * 1024**2
    file_id = init.data["file"]["id"]
    row = ResearchFile.objects.get(pk=file_id)
    assert row.multipart_upload_id

    parts = client.post(f"{API}/uploads/{file_id}/parts/", {"part_numbers": [1, 2, 3]}, format="json")
    assert parts.status_code == 200
    assert [p["part_number"] for p in parts.data["parts"]] == [1, 2, 3]
    assert all(p["url"].startswith("https://fake-s3.test/upload_part/") for p in parts.data["parts"])

    chunks = [body[:200], body[200:400], body[400:]]
    etags = [fake_s3.put_part(row.multipart_upload_id, i + 1, c) for i, c in enumerate(chunks)]
    gap = client.post(
        f"{API}/uploads/{file_id}/complete/",
        {"parts": [{"part_number": 1, "etag": etags[0]}, {"part_number": 3, "etag": etags[2]}]},
        format="json",
    )
    assert gap.status_code == 400

    done = client.post(
        f"{API}/uploads/{file_id}/complete/",
        {"parts": [{"part_number": i + 1, "etag": e} for i, e in enumerate(etags)]},
        format="json",
    )
    assert done.status_code == 200, done.data
    row.refresh_from_db()
    assert row.status == FileStatus.AVAILABLE
    assert row.multipart_upload_id == ""
    assert row.etag_is_md5 is False
    assert fake_s3.objects[row.storage_key]["body"] == body


def test_multipart_parts_request_validation(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MULTIPART_THRESHOLD = 100
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="big.bin", size=1000).data["file"]["id"]
    assert client.post(f"{API}/uploads/{file_id}/parts/", {"part_numbers": []}, format="json").status_code == 400
    assert client.post(f"{API}/uploads/{file_id}/parts/", {"part_numbers": [0]}, format="json").status_code == 400
    assert client.post(f"{API}/uploads/{file_id}/parts/", {"part_numbers": list(range(1, 102))}, format="json").status_code == 400


def test_multipart_expired_upload(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MULTIPART_THRESHOLD = 100
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="big.bin", size=1000).data["file"]["id"]
    fake_s3.multipart.clear()
    done = client.post(f"{API}/uploads/{file_id}/complete/", {"parts": [{"part_number": 1, "etag": "x"}]}, format="json")
    assert done.status_code == 409
    assert done.data["code"] == "upload_expired"
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.FAILED


# ---------------------------------------------------------------- validation / type policy


@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "script.ps1", "evil.exe.", "dir\\payload.msi", "x.lnk"])
def test_blocked_extensions_rejected_at_initiate(student, workspace, fake_s3, name):
    resp = _initiate(make_client(student), workspace["id"], filename=name, size=10)
    assert resp.status_code == 400
    assert resp.data["code"] == "blocked_extension"
    assert not ResearchFile.objects.exists()


def test_executable_content_disguised_as_pdf_is_rejected(student, workspace, fake_s3):
    client = make_client(student)
    file_id, done = upload_file(client, fake_s3, workspace["id"], "thesis.pdf", EXE)
    assert done.status_code == 422
    row = ResearchFile.objects.get(pk=file_id)
    assert row.status == FileStatus.FAILED
    assert row.storage_key not in fake_s3.objects


def test_renaming_to_blocked_extension_rejected(student, workspace, fake_s3):
    client = make_client(student)
    file_id, _ = upload_file(client, fake_s3, workspace["id"], "a.pdf", PDF)
    resp = client.patch(f"{API}/files/{file_id}/", {"name": "a.exe"}, format="json")
    assert resp.status_code == 400
    assert ResearchFile.objects.get(pk=file_id).display_name == "a.pdf"


@pytest.mark.parametrize(
    "raw,expected_display",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\win.ini", "win.ini"),
        ("C:\\Users\\me\\data.csv", "data.csv"),
        ("bad\x00name\x1f.txt", "badname.txt"),
        ("  spaced.txt  ", "spaced.txt"),
    ],
)
def test_malicious_filenames_are_neutralised(student, workspace, fake_s3, raw, expected_display):
    resp = _initiate(make_client(student), workspace["id"], filename=raw, size=5)
    assert resp.status_code == 201, resp.data
    assert resp.data["file"]["name"] == expected_display
    row = ResearchFile.objects.get(pk=resp.data["file"]["id"])
    key = row.storage_key
    assert ".." not in key and "\\" not in key and "\x00" not in key
    assert "\x00" not in row.original_name
    assert key.startswith(f"research/workspaces/{workspace['id']}/files/")


def test_empty_filename_rejected(student, workspace, fake_s3):
    for bad in ("", "...", "/", "\x00"):
        assert _initiate(make_client(student), workspace["id"], filename=bad, size=5).status_code == 400


def test_unicode_filename_kept_for_display_and_ascii_in_key(student, workspace, fake_s3):
    resp = _initiate(make_client(student), workspace["id"], filename="Résumé données.pdf", size=5)
    assert resp.data["file"]["name"] == "Résumé données.pdf"
    key = ResearchFile.objects.get(pk=resp.data["file"]["id"]).storage_key
    assert key.endswith("/Resume_donnees.pdf")
    key.encode("ascii")


def test_file_size_limits(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MAX_FILE_SIZE = 1000
    client = make_client(student)
    too_big = _initiate(client, workspace["id"], filename="a.pdf", size=1001)
    assert too_big.status_code == 413
    assert too_big.data["code"] == "file_too_large"
    assert _initiate(client, workspace["id"], filename="a.pdf", size=-1).status_code == 400
    assert _initiate(client, workspace["id"], filename="a.pdf", size="abc").status_code == 400


def test_workspace_and_user_quota(settings, student, workspace, fake_s3):
    client = make_client(student)
    upload_file(client, fake_s3, workspace["id"], "a.pdf", PDF)
    settings.MY_RESEARCH_WORKSPACE_STORAGE_QUOTA = len(PDF) + 10
    resp = _initiate(client, workspace["id"], filename="b.pdf", size=20)
    assert resp.status_code == 413
    assert resp.data["code"] == "quota_exceeded"
    settings.MY_RESEARCH_WORKSPACE_STORAGE_QUOTA = 0
    settings.MY_RESEARCH_USER_STORAGE_QUOTA = len(PDF) + 10
    assert _initiate(client, workspace["id"], filename="b.pdf", size=20).status_code == 413
    assert _initiate(client, workspace["id"], filename="b.pdf", size=5).status_code == 201


def test_pending_upload_limit(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MAX_PENDING_UPLOADS_PER_USER = 2
    client = make_client(student)
    assert _initiate(client, workspace["id"], filename="1.pdf", size=5).status_code == 201
    assert _initiate(client, workspace["id"], filename="2.pdf", size=5).status_code == 201
    resp = _initiate(client, workspace["id"], filename="3.pdf", size=5)
    assert resp.status_code == 429


def test_html_uploaded_as_pdf_is_never_served_inline(student, workspace, fake_s3):
    client = make_client(student)
    html = b"<html><script>alert(document.cookie)</script></html>"
    file_id, done = upload_file(client, fake_s3, workspace["id"], "report.pdf", html, content_type="application/pdf")
    assert done.status_code == 200
    assert done.data["detected_type"] == "text"
    assert done.data["preview_kind"] == "none"
    resp = client.post(f"{API}/files/{file_id}/download/", {"disposition": "inline"}, format="json")
    assert resp.data["disposition"] == "attachment"
    assert resp.data["content_type"] == "application/octet-stream"
    params = fake_s3.presigned[-1]["params"]
    assert params["ResponseContentType"] == "application/octet-stream"
    assert params["ResponseContentDisposition"].startswith("attachment;")
    assert params["ResponseCacheControl"] == "private, no-store"


def test_png_served_inline_and_filename_header_is_safe(student, workspace, fake_s3):
    client = make_client(student)
    file_id, _ = upload_file(client, fake_s3, workspace["id"], 'img"; x=1.png', PNG)
    resp = client.post(f"{API}/files/{file_id}/download/", {"disposition": "inline"}, format="json")
    assert resp.data["disposition"] == "inline"
    assert resp.data["content_type"] == "image/png"
    header = fake_s3.presigned[-1]["params"]["ResponseContentDisposition"]
    assert header.count('"') == 2
    assert "\n" not in header


def test_csv_text_preview(student, workspace, fake_s3):
    client = make_client(student)
    file_id, _ = upload_file(client, fake_s3, workspace["id"], "spectrum.csv", CSV)
    resp = client.get(f"{API}/files/{file_id}/preview/")
    assert resp.status_code == 200
    assert resp.data["kind"] == "csv"
    assert resp.data["content"] == CSV.decode()
    assert resp.data["truncated"] is False

    pdf_id, _ = upload_file(client, fake_s3, workspace["id"], "a.pdf", PDF)
    assert client.get(f"{API}/files/{pdf_id}/preview/").status_code == 400


def test_sniffer_unit_cases():
    assert file_policy.sniff_type(b"%PDF-1.4") == "pdf"
    assert file_policy.sniff_type(b"\xff\xd8\xff\xe0") == "jpeg"
    assert file_policy.sniff_type(b"\x7fELF\x02") == "executable"
    assert file_policy.sniff_type(b"\xcf\xfa\xed\xfe") == "executable"
    assert file_policy.sniff_type(b"PK\x03\x04") == "zip"
    assert file_policy.sniff_type(b"\x89HDF\r\n\x1a\n") == "hdf5"
    assert file_policy.sniff_type(b"") == "empty"
    assert file_policy.sniff_type("naïve text".encode()) == "text"
    assert file_policy.sniff_type(b"\x00\x01\x02\x03") == "binary"


# ---------------------------------------------------------------- cleanup task


def _age(file_id, hours=48):
    ResearchFile.objects.filter(pk=file_id).update(created_at=timezone.now() - timedelta(hours=hours))


def test_cleanup_finalizes_uploads_whose_confirm_was_lost(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    fake_s3.put(ResearchFile.objects.get(pk=file_id).storage_key, PDF)
    _age(file_id)
    assert tasks.cleanup_stale_uploads() == {"finalized": 1, "failed": 0, "skipped": 0}
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.AVAILABLE


def test_cleanup_marks_missing_uploads_failed_and_ignores_fresh_ones(student, workspace, fake_s3):
    client = make_client(student)
    stale = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    fresh = _initiate(client, workspace["id"], filename="b.pdf", size=len(PDF)).data["file"]["id"]
    _age(stale)
    assert tasks.cleanup_stale_uploads() == {"finalized": 0, "failed": 1, "skipped": 0}
    assert ResearchFile.objects.get(pk=stale).status == FileStatus.FAILED
    assert ResearchFile.objects.get(pk=fresh).status == FileStatus.PENDING_UPLOAD


def test_cleanup_skips_on_storage_outage(student, workspace, fake_s3):
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="a.pdf", size=len(PDF)).data["file"]["id"]
    fake_s3.put(ResearchFile.objects.get(pk=file_id).storage_key, PDF)
    _age(file_id)
    fake_s3.fail.add("head_object")
    assert tasks.cleanup_stale_uploads() == {"finalized": 0, "failed": 0, "skipped": 1}
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.PENDING_UPLOAD
    assert ResearchFile.objects.get(pk=file_id).storage_key in fake_s3.objects


def test_cleanup_completes_fully_uploaded_multipart(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MULTIPART_THRESHOLD = 100
    body = PDF + b"y" * 300
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="big.pdf", size=len(body)).data["file"]["id"]
    row = ResearchFile.objects.get(pk=file_id)
    fake_s3.put_part(row.multipart_upload_id, 1, body[:150])
    fake_s3.put_part(row.multipart_upload_id, 2, body[150:])
    _age(file_id)
    assert tasks.cleanup_stale_uploads()["finalized"] == 1
    row.refresh_from_db()
    assert row.status == FileStatus.AVAILABLE
    assert fake_s3.objects[row.storage_key]["body"] == body


def test_cleanup_aborts_incomplete_multipart(settings, student, workspace, fake_s3):
    settings.MY_RESEARCH_MULTIPART_THRESHOLD = 100
    client = make_client(student)
    file_id = _initiate(client, workspace["id"], filename="big.pdf", size=1000).data["file"]["id"]
    row = ResearchFile.objects.get(pk=file_id)
    fake_s3.put_part(row.multipart_upload_id, 1, b"%PDF-" + b"z" * 100)
    _age(file_id)
    assert tasks.cleanup_stale_uploads()["failed"] == 1
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.FAILED
    assert row.multipart_upload_id not in fake_s3.multipart


def test_cleanup_never_touches_available_files(student, workspace, fake_s3):
    client = make_client(student)
    file_id, _ = upload_file(client, fake_s3, workspace["id"], "a.pdf", PDF)
    _age(file_id, hours=24 * 30)
    assert tasks.cleanup_stale_uploads() == {"finalized": 0, "failed": 0, "skipped": 0}
    row = ResearchFile.objects.get(pk=file_id)
    assert row.status == FileStatus.AVAILABLE
    assert row.storage_key in fake_s3.objects


def test_cleanup_periodic_task_seed_and_reverse():
    import importlib

    from django.apps import apps
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    shared_crontab = CrontabSchedule.objects.create(minute="35", hour="*")
    unrelated = PeriodicTask.objects.create(
        name="Unrelated hourly job", task="equipment.some_task", crontab=shared_crontab, enabled=True
    )

    migration = importlib.import_module("iic_booking.my_research.migrations.0001_initial")
    migration.create_cleanup_schedule(apps, None)
    migration.create_cleanup_schedule(apps, None)
    task = PeriodicTask.objects.get(task="my_research.cleanup_stale_uploads")
    assert task.name == "My Research stale upload cleanup (hourly)"
    assert task.enabled is False
    assert task.crontab.minute == "35"
    assert task.crontab.hour == "*"
    assert PeriodicTask.objects.filter(task="my_research.cleanup_stale_uploads").count() == 1

    migration.remove_cleanup_schedule(apps, None)
    assert not PeriodicTask.objects.filter(task="my_research.cleanup_stale_uploads").exists()
    unrelated.refresh_from_db()
    assert unrelated.enabled is True
    assert unrelated.crontab_id == shared_crontab.pk
    assert CrontabSchedule.objects.filter(pk=shared_crontab.pk).exists()


def test_cleanup_schedule_seed_leaves_an_operator_enabled_task_alone():
    import importlib

    from django.apps import apps
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    migration = importlib.import_module("iic_booking.my_research.migrations.0001_initial")
    existing = PeriodicTask.objects.create(
        name=migration.CLEANUP_TASK_NAME,
        task="my_research.cleanup_stale_uploads",
        crontab=CrontabSchedule.objects.create(minute="35", hour="*"),
        enabled=True,
    )
    migration.create_cleanup_schedule(apps, None)
    existing.refresh_from_db()
    assert existing.enabled is True
    assert PeriodicTask.objects.filter(name=migration.CLEANUP_TASK_NAME).count() == 1
