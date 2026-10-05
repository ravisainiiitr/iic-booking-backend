import pytest

from iic_booking.my_research import storage

from .conftest import API, make_client, upload_file


def test_read_prefix_of_empty_object_is_empty(fake_s3):
    fake_s3.put("ws/empty.txt", b"")
    assert storage.read_prefix("ws/empty.txt", 512) == b""


def test_read_prefix_still_reports_missing_objects(fake_s3):
    with pytest.raises(storage.ObjectNotFound):
        storage.read_prefix("ws/missing.txt", 512)


def test_read_prefix_surfaces_other_storage_errors(fake_s3):
    fake_s3.put("ws/a.txt", b"hello")
    fake_s3.fail.add("get_object")
    with pytest.raises(storage.ResearchStorageError):
        storage.read_prefix("ws/a.txt", 512)


@pytest.mark.django_db
def test_empty_file_upload_completes(fake_s3, student, workspace):
    client = make_client(student)
    file_id, done = upload_file(client, fake_s3, workspace["id"], "New Text Document.txt", b"")
    assert done.status_code == 200, done.data
    detail = client.get(f"{API}/files/{file_id}/")
    assert detail.status_code == 200
    assert detail.data["status"] == "AVAILABLE"
