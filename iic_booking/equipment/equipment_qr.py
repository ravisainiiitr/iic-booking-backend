"""Equipment QR codes and the public equipment information endpoint.

- Every equipment's QR code encodes its canonical public page, ``<PUBLIC_EQUIPMENT_BASE_URL>/equipment/<id>``
  (the existing SPA route). The ID never changes, so a printed code survives renames.
- QR images are generated on demand with the ``qrcode`` library already used for certificates (PNG via Pillow, SVG
  as vector paths); nothing is stored and no external service is called.
- ``public_equipment_payload`` is an explicit allowlist of fields for anyone who scans the code. It includes the
  lab contacts (Officers In-charge and Lab Operators: name, phone, email, office, photo; approved for public display
  on 5 Nov 2026) and never charges, bookings, internal notes, storage paths or notification addresses.
- Management endpoints (list, PNG/SVG, ZIP) are for the Main Admin (all equipment) and OICs (equipment they are
  Officer In-charge of, primary or temporary), enforced here.
"""

from __future__ import annotations

import io
import ipaddress
import re
import zipfile
from decimal import Decimal
from decimal import InvalidOperation
from urllib.parse import quote
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .models import Equipment
from .models import EquipmentStatus

MAX_BATCH = 200
PNG_SIZES = {"small": 8, "medium": 16, "large": 32}  # pixels per QR module
QUIET_ZONE = 4  # modules: the QR standard's minimum quiet zone
GOOGLE_MAPS_HOSTS = re.compile(
    r"^(?:(?:www|maps)\.)?google\.[a-z.]{2,6}$|^maps\.app\.goo\.gl$|^goo\.gl$|^g\.page$|^g\.co$", re.I
)


# --------------------------------------------------------------------------- canonical URL
def public_base_url() -> str:
    raw = getattr(settings, "PUBLIC_EQUIPMENT_BASE_URL", "") or getattr(settings, "FRONTEND_URL", "") or ""
    return str(raw).strip().rstrip("/")


def public_equipment_url(equipment_id: int) -> str:
    return f"{public_base_url()}/equipment/{int(equipment_id)}"


def base_url_warning() -> str:
    """Why the configured base URL should not be printed on QR codes, or "" when it is fine."""
    base = public_base_url()
    parsed = urlparse(base)
    if not base or parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "The public site address (PUBLIC_EQUIPMENT_BASE_URL / FRONTEND_URL) is not set to a valid URL."
    host = parsed.hostname.lower()
    local = host == "localhost" or host.endswith(".localhost") or host.endswith(".local")
    try:
        local = local or ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if local:
        return f"QR codes point to {base}, a local address. Set PUBLIC_EQUIPMENT_BASE_URL before printing."
    if parsed.scheme != "https" and not settings.DEBUG:
        return f"QR codes point to {base} without HTTPS."
    return ""


# --------------------------------------------------------------------------- QR images
def _qr(data: str):
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=QUIET_ZONE, box_size=16)
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def qr_png(data: str, size: str = "medium") -> bytes:
    qr = _qr(data)
    qr.box_size = PNG_SIZES.get(size, PNG_SIZES["medium"])
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_svg(data: str) -> bytes:
    from qrcode.image.svg import SvgPathImage

    img = _qr(data).make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def _filename(eq: Equipment, ext: str) -> str:
    code = re.sub(r"[^A-Za-z0-9._-]+", "-", eq.code or "").strip("-")[:60] or "equipment"
    return f"equipment-{eq.pk}-{code}-qr.{ext}"


# --------------------------------------------------------------------------- directions
def safe_google_maps_url(raw: str | None) -> str:
    """The stored Google Maps link, only when it is an https/http link on a Google Maps host (no other redirects)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if not GOOGLE_MAPS_HOSTS.match(parsed.hostname):
        return ""
    if parsed.hostname.lower().startswith("google.") or parsed.hostname.lower().startswith("www.google."):
        if not parsed.path.startswith("/maps"):
            return ""
    return raw.replace("http://", "https://", 1) if parsed.scheme == "http" else raw


def _coord(value, limit: int):
    if value in (None, ""):
        return None
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return v if -limit <= v <= limit else None


def directions(eq: Equipment) -> dict:
    lat, lng = _coord(eq.latitude, 90), _coord(eq.longitude, 180)
    has_coords = lat is not None and lng is not None and not (lat == 0 and lng == 0)
    maps_url = safe_google_maps_url(eq.google_maps_url)
    coords = f"{lat.normalize()},{lng.normalize()}" if has_coords else ""
    if maps_url:
        url, source = maps_url, "maps_url"
    elif has_coords:
        url, source = f"https://www.google.com/maps/dir/?api=1&destination={quote(coords)}", "coordinates"
    else:
        url, source = "", ""
    return {
        "maps_url": url,
        "source": source,
        "latitude": float(lat) if has_coords else None,
        "longitude": float(lng) if has_coords else None,
        # Key-less embed from coordinates only; the page works without it when embeds are blocked.
        "embed_url": f"https://www.google.com/maps?q={quote(coords)}&z=17&output=embed" if has_coords else "",
    }


# --------------------------------------------------------------------------- public payload
def publicly_visible(eq: Equipment) -> bool:
    """Same rule the catalog applies to an anonymous visitor (no visibility group, department visible, not
    test-only, base/mode catalog rules relaxed as on the read-only equipment page)."""
    from .api_views import user_can_see_equipment

    return user_can_see_equipment(AnonymousUser(), eq, mode_rules=False)


PUBLIC_STATUS = {
    EquipmentStatus.ACTIVE: ("operational", "Operational"),
}


def public_status(eq: Equipment) -> dict:
    """Coarse status only: Operational, or "Temporarily unavailable" for any other state (no maintenance detail)."""
    code = (eq.status or "").strip()
    key, label = PUBLIC_STATUS.get(code, ("unavailable", "Temporarily unavailable"))
    return {"key": key, "label": label}


def public_equipment_payload(eq: Equipment, request=None) -> dict:
    from .serializers import _equipment_image_url

    specs = [
        {"label": s.spec_key.strip(), "value": s.spec_value.strip()}
        for s in eq.equipment_specifications.all().order_by("equipment_specification_id")
        if (s.spec_key or "").strip() and (s.spec_value or "").strip()
    ]
    accessories = [
        {"name": a.accessory_name, "optional": a.is_optional}
        for a in eq.equipment_accessories.filter(is_enabled=True).order_by("accessory_name")
        if (a.accessory_name or "").strip()
    ]
    dept = eq.internal_department
    return {
        "equipment_id": eq.pk,
        "code": eq.code,
        "name": eq.name,
        "description": (eq.description or "").strip(),
        "important_instruction": (eq.important_instruction or "").strip(),
        "status": public_status(eq),
        "category": eq.category.name if eq.category_id else "",
        "department": {"name": dept.name, "code": dept.code or ""} if dept else None,
        "make": (eq.make or "").strip(),
        "model": (eq.model_information or "").strip(),
        "image_url": _equipment_image_url(eq, request=request, verify_storage=False),
        "specifications": specs,
        "accessories": accessories,
        "location": (eq.location or "").strip(),
        "directions": directions(eq),
        "enquiry_address": (eq.office_address or "").strip(),
        "contacts": public_contacts(eq, request),
        "public_url": public_equipment_url(eq.pk),
        "booking_path": f"/book-equipment?equipment_id={eq.pk}",
        "charges_path": "/analysis-charges",
    }


def _contact(role: str, name, email, phone, alternate, office, photo) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    phones = [p for p in dict.fromkeys([(phone or "").strip(), (alternate or "").strip()]) if p]
    return {"role": role, "name": name, "email": (email or "").strip(), "phones": phones,
            "office": (office or "").strip(), "photo_url": photo or ""}


def public_contacts(eq: Equipment, request=None) -> list[dict]:
    """Officers In-charge, then Lab Operators, exactly as the signed-in equipment page lists them (same serializers,
    so secondary-operator cover is applied the same way). Only these contact fields leave the server."""
    from .serializers import EquipmentDetailSerializer, EquipmentManagerSerializer

    ctx = {"request": request}
    out = []
    managers = eq.equipment_managers.select_related("manager").order_by("equipment_manager_id")
    for m in EquipmentManagerSerializer(managers, many=True, context=ctx).data:
        out.append(_contact("Officer In-charge", m.get("manager_name"), m.get("manager_email"), m.get("manager_phone"),
                            m.get("alternate_phone_number"), m.get("office_address"), m.get("manager_profile_picture")))
    for o in EquipmentDetailSerializer(context=ctx).get_operators(eq):
        out.append(_contact("Lab Operator", o.get("operator_name"), o.get("operator_email"), o.get("operator_phone"),
                            o.get("alternate_phone_number"), o.get("office_address"), o.get("operator_profile_picture")))
    seen, unique = set(), []
    for c in out:
        if c and (c["role"], c["name"], c["email"]) not in seen:
            seen.add((c["role"], c["name"], c["email"]))
            unique.append(c)
    return unique


@api_view(["GET"])
@authentication_classes([])  # the same allowlisted answer for everyone; stale tokens never break a scan
@permission_classes([AllowAny])
def public_equipment(request, pk: int):
    eq = (Equipment.objects.select_related("internal_department", "category").filter(pk=pk).first())
    if eq is None or not publicly_visible(eq):
        # One answer for "missing" and "not public", so restricted equipment is not revealed.
        return Response({"error": "This equipment page is not available.", "code": "not_available"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(public_equipment_payload(eq, request=request))


# --------------------------------------------------------------------------- management scope
def qr_scope_queryset(user):
    """Equipment the user may manage QR codes for: all for the Main Admin, managed equipment for an OIC."""
    from .reports import get_equipment_ids_managed_by_oic

    t = getattr(user, "user_type", None)
    qs = Equipment.objects.select_related("internal_department", "category")
    if t == UserType.ADMIN:
        return qs
    if t == UserType.MANAGER:
        return qs.filter(pk__in=list(get_equipment_ids_managed_by_oic(user.id)))
    return qs.none()


def _can_manage_qr(user) -> bool:
    return getattr(user, "user_type", None) in (UserType.ADMIN, UserType.MANAGER)


def _forbidden():
    return Response({"error": "Equipment QR codes are for the Main Administrator and Officers In-charge.",
                     "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)


def qr_row(eq: Equipment) -> dict:
    dept = eq.internal_department
    return {
        "equipment_id": eq.pk,
        "code": eq.code,
        "name": eq.name,
        "make": eq.make or "",
        "model": eq.model_information or "",
        "category": eq.category.name if eq.category_id else "",
        "department": {"id": dept.pk, "name": dept.name, "code": dept.code or ""} if dept else None,
        "status": eq.status or "",
        "status_display": eq.get_status_display() if eq.status else "",
        "public_url": public_equipment_url(eq.pk),
        "publicly_visible": publicly_visible(eq),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_qr_list(request):
    """?q= (name, code, ID, make, model), ?department_id=, ?status=, ?ids=1,2,3."""
    if not _can_manage_qr(request.user):
        return _forbidden()
    qs = qr_scope_queryset(request.user)
    q = (request.query_params.get("q") or "").strip()
    if q:
        cond = (Q(name__icontains=q) | Q(code__icontains=q) | Q(make__icontains=q)
                | Q(model_information__icontains=q) | Q(category__name__icontains=q))
        if q.isdigit():
            cond |= Q(pk=int(q))
        qs = qs.filter(cond)
    dept = request.query_params.get("department_id")
    if dept and str(dept).isdigit():
        qs = qs.filter(internal_department_id=int(dept))
    st = (request.query_params.get("status") or "").strip()
    if st:
        qs = qs.filter(status=st)
    ids = _parse_ids(request.query_params.get("ids"))
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    rows = [qr_row(e) for e in qs.order_by("name", "pk")[:1000]]
    depts = {}
    for e in qr_scope_queryset(request.user).exclude(internal_department__isnull=True):
        depts[e.internal_department_id] = {"id": e.internal_department_id, "name": e.internal_department.name,
                                           "code": e.internal_department.code or ""}
    return Response({
        "results": rows,
        "count": len(rows),
        "departments": sorted(depts.values(), key=lambda d: d["name"]),
        "statuses": [{"value": v, "label": str(lbl)} for v, lbl in EquipmentStatus.choices],
        "base_url": public_base_url(),
        "base_url_warning": base_url_warning(),
    })


def _parse_ids(raw) -> list[int] | None:
    if raw in (None, ""):
        return None
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out[:MAX_BATCH]


def _scoped_equipment(request, pk: int):
    if not _can_manage_qr(request.user):
        return None, _forbidden()
    eq = qr_scope_queryset(request.user).filter(pk=pk).first()
    if eq is None:
        return None, Response({"error": "Equipment not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    return eq, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_qr_image(request, pk: int, fmt: str):
    """PNG (?size=small|medium|large) or SVG of the QR code for one equipment; ?download=1 saves it as a file."""
    eq, denied = _scoped_equipment(request, pk)
    if denied:
        return denied
    url = public_equipment_url(eq.pk)
    if fmt == "svg":
        resp = HttpResponse(qr_svg(url), content_type="image/svg+xml")
    else:
        resp = HttpResponse(qr_png(url, request.query_params.get("size") or "medium"), content_type="image/png")
    resp["Cache-Control"] = "private, max-age=300"
    resp["X-QR-URL"] = url
    disposition = "attachment" if request.query_params.get("download") in ("1", "true") else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="{_filename(eq, fmt)}"'
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_qr_zip(request):
    """ZIP with a PNG and an SVG for each requested equipment (?ids=1,2,3; at most 200) within the user's scope.
    IDs outside the scope are skipped and listed in the README."""
    if not _can_manage_qr(request.user):
        return _forbidden()
    ids = _parse_ids(request.query_params.get("ids")) or []
    if not ids:
        return Response({"error": "Choose at least one equipment.", "code": "no_ids"}, status=status.HTTP_400_BAD_REQUEST)
    rows = list(qr_scope_queryset(request.user).filter(pk__in=ids).order_by("name", "pk"))
    found = {e.pk for e in rows}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        lines = ["Equipment QR codes", "", "ID\tCode\tName\tPublic URL"]
        for e in rows:
            url = public_equipment_url(e.pk)
            zf.writestr(_filename(e, "png"), qr_png(url, "large"))
            zf.writestr(_filename(e, "svg"), qr_svg(url))
            lines.append(f"{e.pk}\t{e.code}\t{e.name}\t{url}")
        skipped = [i for i in ids if i not in found]
        if skipped:
            lines += ["", "Skipped (not found or outside your equipment): " + ", ".join(map(str, skipped))]
        zf.writestr("README.txt", "\n".join(lines) + "\n")
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="equipment-qr-codes.zip"'
    return resp
