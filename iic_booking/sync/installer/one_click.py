"""
One-click DSA installer download.

Deployment Center → "Download one-click installer": a Department/Main Admin asks for a ticket
(authenticated POST), the browser follows the ticket URL, and the portal streams the latest DSA
installer with a small trailer appended:

    <installer bytes> <UTF-8 JSON> <uint32 little-endian JSON length> <8-byte magic "IICDSAK1">

The JSON carries the portal URL, the department and a fresh single-use setup key
(``device_provisioning.setup_keys``). The installer reads its own trailer, registers its
provisioning session with that key and installs with no sign-in or questions. Windows ignores
bytes after the PE image, and the .NET single-file bundle locates its payload through the
apphost header, so the appended trailer does not affect the program.

The key is minted when the file is downloaded (never in a URL), the ticket is single use and
expires in 10 minutes, and the key itself is single use and expires in at most 24 hours.
Note: appending bytes invalidates an Authenticode signature; when the installer is code-signed
this must move into the certificate table's unauthenticated area.
"""

from __future__ import annotations

import json
import secrets
import struct

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.device_provisioning import policy as policy_mod
from iic_booking.device_provisioning import setup_keys
from iic_booking.device_provisioning.models import DeviceType
from iic_booking.sync.installer.models import DsaInstallerRelease
from iic_booking.sync.permissions import CanManageDepartmentSync

TRAILER_MAGIC = b"IICDSAK1"
_SALT = "iic-dsa-one-click-v1"
_TICKET_MAX_AGE = 10 * 60
_CHUNK = 1024 * 1024


def build_trailer(payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return body + struct.pack("<I", len(body)) + TRAILER_MAGIC


def parse_trailer(blob: bytes) -> dict | None:
    """Inverse of build_trailer for the tail of a file (used by tests)."""
    if len(blob) < 12 or not blob.endswith(TRAILER_MAGIC):
        return None
    (length,) = struct.unpack("<I", blob[-12:-8])
    if length <= 0 or length > len(blob) - 12:
        return None
    return json.loads(blob[-12 - length : -12].decode("utf-8"))


def _latest_release():
    return (
        DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )


def _resolve_department(request):
    from iic_booking.users.models import Department

    raw = request.data.get("department_id")
    if raw not in (None, ""):
        try:
            return Department.objects.filter(pk=int(raw)).first()
        except (TypeError, ValueError):
            return None
    return getattr(request.user, "department", None)


@api_view(["POST"])
@permission_classes([IsAuthenticated, CanManageDepartmentSync])
def one_click_ticket(request):
    """POST {department_id?, ttl_hours?} → {url, expires_in, filename, version}."""
    rel = _latest_release()
    if not rel or not rel.file:
        return Response({"detail": "No installer release published."}, status=status.HTTP_404_NOT_FOUND)

    department = _resolve_department(request)
    if department is None:
        return Response({"detail": "Choose a department."}, status=status.HTTP_400_BAD_REQUEST)
    user = request.user
    if not (
        policy_mod.user_belongs_to_department(user, department)
        and policy_mod.user_has_provisioning_rights(user, department)
    ):
        return Response(
            {"detail": "You cannot provision devices for this department."},
            status=status.HTTP_403_FORBIDDEN,
        )

    ttl = request.data.get("ttl_hours")
    try:
        ttl_hours = float(ttl) if ttl not in (None, "") else setup_keys.DEFAULT_TTL.total_seconds() / 3600
    except (TypeError, ValueError):
        return Response({"detail": "ttl_hours must be a number."}, status=status.HTTP_400_BAD_REQUEST)
    ttl_hours = max(0.25, min(ttl_hours, setup_keys.MAX_TTL.total_seconds() / 3600))

    token = signing.dumps(
        {
            "r": str(rel.id),
            "u": str(user.pk),
            "d": department.pk,
            "t": ttl_hours,
            "n": secrets.token_urlsafe(12),
        },
        salt=_SALT,
        compress=True,
    )
    url = request.build_absolute_uri(f"/api/v1/sync/installer/releases/one-click/{token}/")
    return Response(
        {
            "url": url,
            "expires_in": _TICKET_MAX_AGE,
            "filename": _filename(department),
            "version": rel.version,
            "key_ttl_hours": ttl_hours,
            "department": getattr(department, "code", "") or getattr(department, "name", ""),
        },
    )


def _filename(department) -> str:
    code = "".join(ch for ch in str(getattr(department, "code", "") or "") if ch.isalnum()) or "Dept"
    return f"DepartmentSyncAgentSetup-{code}-OneClick.exe"


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def one_click_download(request, token: str):
    from iic_booking.users.models import Department

    try:
        data = signing.loads(token, salt=_SALT, max_age=_TICKET_MAX_AGE)
    except signing.SignatureExpired:
        return Response({"detail": "Download link expired. Please try again."}, status=status.HTTP_403_FORBIDDEN)
    except signing.BadSignature:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    # Single use: a forwarded or replayed link cannot mint more keys.
    if not cache.add(f"dsa-one-click:{data.get('n')}", 1, timeout=_TICKET_MAX_AGE + 60):
        return Response({"detail": "This download link was already used."}, status=status.HTTP_403_FORBIDDEN)

    rel = DsaInstallerRelease.objects.filter(pk=data.get("r"), is_active=True).first()
    if not rel or not rel.file:
        return Response({"detail": "Installer release not found."}, status=status.HTTP_404_NOT_FOUND)
    user = get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()
    department = Department.objects.filter(pk=data.get("d")).first()
    if user is None or department is None:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    try:
        key, plaintext = setup_keys.issue(
            actor=user,
            department=department,
            device_type=DeviceType.DSA,
            ttl_hours=data.get("t"),
            client_ip=request.META.get("REMOTE_ADDR"),
        )
    except PermissionError:
        return Response(
            {"detail": "You cannot provision devices for this department."},
            status=status.HTTP_403_FORBIDDEN,
        )

    portal = request.build_absolute_uri("/").rstrip("/")
    trailer = build_trailer(
        {
            "v": 1,
            "portal": portal,
            "setup_key": plaintext,
            "department_id": department.pk,
            "department": getattr(department, "code", "") or getattr(department, "name", ""),
            "expires_at": key.expires_at.isoformat(),
            "version": rel.version,
        },
    )

    field = rel.file
    try:
        size = int(field.size)
    except Exception:  # noqa: BLE001 — storage without size → no Content-Length
        size = None

    def stream():
        with field.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        yield trailer

    response = StreamingHttpResponse(stream(), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{_filename(department)}"'
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    if size is not None:
        response["Content-Length"] = str(size + len(trailer))
    return response


__all__ = ["TRAILER_MAGIC", "build_trailer", "one_click_download", "one_click_ticket", "parse_trailer"]
