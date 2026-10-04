import io
import zipfile

import pytest
from asgiref.sync import async_to_sync

from iic_booking.my_research import archive

from .conftest import API, make_client, upload_file

pytestmark = pytest.mark.django_db

ROOT = "Development of Nanocomposite Coating"


def _collect(response) -> bytes:
    if response.is_async:
        async def gather():
            return b"".join([part async for part in response.streaming_content])

        return async_to_sync(gather)()
    return b"".join(response.streaming_content)


def _folder(client, workspace_id, name, parent_id=None):
    resp = client.post(f"{API}/workspaces/{workspace_id}/folders/", {"name": name, "parent_id": parent_id}, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data["id"]


@pytest.fixture
def project(fake_s3, student, workspace):
    client = make_client(student)
    ws = workspace["id"]
    booking = _folder(client, ws, "IICFESEM0001")
    raw = _folder(client, ws, "Raw Data", booking)
    _folder(client, ws, "Processed Data", booking)
    for name, body, folder in [
        ("notes.txt", b"project notes", None),
        ("summary.csv", b"a,b\n1,2\n", booking),
        ("scan.tif", b"\x00\x01" * 5000, raw),
        ("empty.txt", b"", raw),
    ]:
        _, done = upload_file(client, fake_s3, ws, name, body, **({"folder_id": folder} if folder else {}))
        assert done.status_code == 200, done.data
    return {"client": client, "ws": ws, "booking": booking, "raw": raw}


def _download(client, ws, folder_id=None):
    resp = client.post(f"{API}/workspaces/{ws}/download-zip/", {"folder_id": folder_id}, format="json")
    assert resp.status_code == 200, resp.data
    got = client.get(f"/api{resp.data['path']}")
    assert got.status_code == 200
    assert got["Content-Type"] == "application/zip"
    return resp.data, got, zipfile.ZipFile(io.BytesIO(_collect(got)))


def test_whole_project_zip_keeps_the_folder_tree(project):
    meta, resp, zf = _download(project["client"], project["ws"])
    assert meta["file_count"] == 4
    assert meta["filename"] == f"{ROOT}.zip"
    assert "attachment" in resp["Content-Disposition"]
    assert zf.testzip() is None
    names = set(zf.namelist())
    assert {
        f"{ROOT}/notes.txt",
        f"{ROOT}/IICFESEM0001/summary.csv",
        f"{ROOT}/IICFESEM0001/Raw Data/scan.tif",
        f"{ROOT}/IICFESEM0001/Raw Data/empty.txt",
        f"{ROOT}/IICFESEM0001/Processed Data/",
    } <= names
    assert zf.read(f"{ROOT}/IICFESEM0001/Raw Data/scan.tif") == b"\x00\x01" * 5000
    assert zf.read(f"{ROOT}/IICFESEM0001/Raw Data/empty.txt") == b""


def test_folder_zip_contains_only_that_folder(project):
    meta, _, zf = _download(project["client"], project["ws"], project["booking"])
    assert meta["file_count"] == 3
    assert meta["filename"] == "IICFESEM0001.zip"
    names = set(zf.namelist())
    assert "IICFESEM0001/summary.csv" in names
    assert "IICFESEM0001/Raw Data/scan.tif" in names
    assert not any(n.endswith("notes.txt") for n in names)


def test_link_works_once(project):
    resp = project["client"].post(f"{API}/workspaces/{project['ws']}/download-zip/", {}, format="json")
    first = project["client"].get(f"/api{resp.data['path']}")
    _collect(first)
    again = project["client"].get(f"/api{resp.data['path']}")
    assert again.status_code == 410


def test_tampered_link_is_rejected(project):
    resp = project["client"].post(f"{API}/workspaces/{project['ws']}/download-zip/", {}, format="json")
    assert project["client"].get(f"/api{resp.data['path'][:-3]}x/").status_code == 410


def test_other_users_cannot_request_a_zip(project, other_student):
    resp = make_client(other_student).post(f"{API}/workspaces/{project['ws']}/download-zip/", {}, format="json")
    assert resp.status_code == 404
    assert make_client().post(f"{API}/workspaces/{project['ws']}/download-zip/", {}, format="json").status_code in (401, 403)


def test_empty_project_has_nothing_to_download(fake_s3, student, workspace):
    resp = make_client(student).post(f"{API}/workspaces/{workspace['id']}/download-zip/", {}, format="json")
    assert resp.status_code == 400
    assert resp.data["code"] == "zip_empty"


def test_size_limits_are_enforced(project, settings):
    settings.MY_RESEARCH_ZIP_MAX_FILES = 2
    resp = project["client"].post(f"{API}/workspaces/{project['ws']}/download-zip/", {}, format="json")
    assert resp.status_code == 413
    assert resp.data["code"] == "zip_too_large"


def test_duplicate_and_unsafe_names_are_made_safe():
    taken: set[str] = set()
    assert archive._unique("a.txt", taken) == "a.txt"
    assert archive._unique("A.txt", taken) == "A (2).txt"
    for hostile in ("../..", "..", "a/../b", "C:\\x"):
        cleaned = archive.safe_component(hostile, "file")
        assert "/" not in cleaned and "\\" not in cleaned and cleaned not in {".", ".."}
    assert archive.safe_component("..", "file") == "file"
    assert archive.safe_component('re:port?.csv', "file") == "re_port_.csv"
