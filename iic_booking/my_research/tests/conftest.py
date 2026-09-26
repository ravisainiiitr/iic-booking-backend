import base64
import hashlib
import io
import uuid
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment, EquipmentPublicationClaim
from iic_booking.my_research import storage
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

API = "/api/v1/my-research"


class FakeS3:
    """In-memory stand-in for the boto3 S3 client calls My Research makes."""

    def __init__(self):
        self.objects = {}
        self.multipart = {}
        self.fail = set()
        self.presigned = []

    def _check(self, op):
        if op in self.fail:
            raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, op)

    @staticmethod
    def _missing(op):
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, op)

    def put(self, key, body: bytes, checksum_b64: str = ""):
        self.objects[key] = {"body": body, "etag": hashlib.md5(body).hexdigest(), "checksum": checksum_b64}

    def generate_presigned_url(self, op, Params, ExpiresIn, HttpMethod=None):
        self._check("presign")
        self.presigned.append({"op": op, "params": Params, "expires": ExpiresIn})
        return f"https://fake-s3.test/{op}/{Params['Key']}?expires={ExpiresIn}"

    def head_object(self, Bucket, Key, ChecksumMode=None):
        self._check("head_object")
        obj = self.objects.get(Key)
        if obj is None:
            raise self._missing("HeadObject")
        resp = {"ContentLength": len(obj["body"]), "ETag": f'"{obj["etag"]}"'}
        if ChecksumMode == "ENABLED" and obj["checksum"]:
            resp["ChecksumSHA256"] = obj["checksum"]
        return resp

    def get_object(self, Bucket, Key, Range=None):
        self._check("get_object")
        obj = self.objects.get(Key)
        if obj is None:
            raise self._missing("GetObject")
        body = obj["body"]
        if Range:
            end = int(Range.split("-")[1])
            body = body[: end + 1]
        return {"Body": io.BytesIO(body)}

    def delete_object(self, Bucket, Key):
        self._check("delete_object")
        self.objects.pop(Key, None)

    def create_multipart_upload(self, Bucket, Key, ContentType=None, **kwargs):
        self._check("create_multipart_upload")
        upload_id = f"up-{uuid.uuid4().hex[:8]}"
        self.multipart[upload_id] = {"key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def put_part(self, upload_id, number, body: bytes):
        etag = hashlib.md5(body).hexdigest()
        self.multipart[upload_id]["parts"][number] = {"body": body, "etag": etag}
        return f'"{etag}"'

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker=0):
        if UploadId not in self.multipart:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        parts = self.multipart[UploadId]["parts"]
        return {
            "Parts": [
                {"PartNumber": n, "ETag": f'"{p["etag"]}"', "Size": len(p["body"])} for n, p in sorted(parts.items())
            ],
            "IsTruncated": False,
        }

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self._check("complete_multipart_upload")
        upload = self.multipart.pop(UploadId, None)
        if upload is None:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload")
        body = b"".join(upload["parts"][p["PartNumber"]]["body"] for p in MultipartUpload["Parts"])
        self.objects[Key] = {"body": body, "etag": f"{hashlib.md5(body).hexdigest()}-{len(MultipartUpload['Parts'])}", "checksum": ""}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.multipart.pop(UploadId, None)


@pytest.fixture(autouse=True)
def my_research_settings(settings):
    settings.MY_RESEARCH_ENABLED = True
    settings.MY_RESEARCH_PILOT_EMAILS = ""
    settings.MY_RESEARCH_S3_BUCKET = "test-research-bucket"
    settings.MY_RESEARCH_USER_STORAGE_QUOTA = 0
    settings.MY_RESEARCH_WORKSPACE_STORAGE_QUOTA = 0
    return settings


@pytest.fixture
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    return fake


def make_client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def make_user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    return UserFactory(**kwargs)


def make_department(department_type="internal"):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"MR-Dept-{tag}",
        code=f"MR{tag[:4]}",
        department_type=department_type,
        equipment_booking_enabled=True,
        equipment_visibility_enabled=True,
    )


def make_equipment(department=None, **kwargs):
    defaults = {
        "name": f"MR EQ {uuid.uuid4().hex[:4]}",
        "code": f"MR{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def make_booking(user, equipment, status=BookingStatus.COMPLETED):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("123.45"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
    )


def make_claim(user, title="Nanocomposite coatings for corrosion resistance", equipment=None):
    claim = EquipmentPublicationClaim.objects.create(
        submitted_by=user, title=title, authors="A. Student, B. Faculty", journal="J. Mater. Sci.", year=2026,
        doi="10.1000/xyz123",
    )
    if equipment is not None:
        claim.equipments.add(equipment)
    return claim


def sha256_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


@pytest.fixture
def internal_dept(db):
    return make_department("internal")


@pytest.fixture
def student(internal_dept):
    return make_user(user_type=UserType.STUDENT, department=internal_dept, name="Rahul Sharma")


@pytest.fixture
def faculty(internal_dept):
    return make_user(user_type=UserType.FACULTY, department=internal_dept, name="Dr. ABC")


@pytest.fixture
def other_student(internal_dept):
    return make_user(user_type=UserType.STUDENT, department=internal_dept, name="Other Student")


@pytest.fixture
def external_user(db):
    return make_user(user_type=UserType.EXTERNAL, name="External Researcher")


@pytest.fixture
def equipment(internal_dept):
    return make_equipment(internal_dept, name="FE-SEM", code=f"FESEM{uuid.uuid4().hex[:4].upper()}")


@pytest.fixture
def workspace(student):
    resp = make_client(student).post(f"{API}/workspaces/", {"name": "Development of Nanocomposite Coating"}, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data


def upload_file(client, fake, workspace_id, name, body: bytes, **extra):
    """Full single-PUT upload through the API with the fake bucket."""
    resp = client.post(
        f"{API}/workspaces/{workspace_id}/uploads/initiate/",
        {"filename": name, "size": len(body), **extra},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    file_id = resp.data["file"]["id"]
    key = fake.presigned[-1]["params"]["Key"]
    fake.put(key, body, checksum_b64=resp.data["upload"]["headers"].get("x-amz-checksum-sha256", ""))
    done = client.post(f"{API}/uploads/{file_id}/complete/", {}, format="json")
    return file_id, done
