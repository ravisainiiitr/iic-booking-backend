"""Public (read-only) API views for CMS."""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from .models import MenuItem, HomePageContent, HeroSlide, CmsPage, SiteDocument


@api_view(["GET"])
@permission_classes([AllowAny])
def menu_list(request):
    """Return menu tree (root items with children). Only active items."""
    from .serializers import MenuItemTreeSerializer

    roots = MenuItem.objects.filter(parent=None, is_active=True).order_by("priority", "id")
    serializer = MenuItemTreeSerializer(roots, many=True)
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def home_page_content(request):
    """Return all home page content as key-value dict plus font_sizes per key."""
    items = HomePageContent.objects.all()
    content = {item.key: item.value for item in items}
    font_sizes = {item.key: (item.font_size or None) for item in items if item.font_size}
    return Response({"content": content, "font_sizes": font_sizes})


@api_view(["GET"])
@permission_classes([AllowAny])
def site_stats(request):
    """
    Public counts for the home hero: all equipment in the institute (except disposed), the same
    for every visitor and role, active user accounts (is_active=True), total bookings, and
    equipment publications.
    """
    from django.contrib.auth import get_user_model

    from iic_booking.equipment.models import Booking, Equipment, EquipmentPublication, EquipmentStatus

    User = get_user_model()
    equipment_count = (
        Equipment.objects.exclude(status=EquipmentStatus.DISPOSED)
        .exclude(visible_to_test_accounts_only=True)
        .count()
    )
    active_users_count = User.objects.filter(is_active=True).count()
    total_bookings_count = Booking.objects.count()
    publication_count = EquipmentPublication.objects.count()
    return Response(
        {
            "equipment_count": equipment_count,
            "active_users_count": active_users_count,
            "total_bookings_count": total_bookings_count,
            "publication_count": publication_count,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def public_publications(request):
    """Public list of equipment-linked publications for the home hero link."""
    from iic_booking.equipment.models import EquipmentPublication

    qs = (
        EquipmentPublication.objects.select_related("equipment")
        .order_by("-year", "title", "equipment_publication_id")
    )
    results = []
    for pub in qs[:500]:
        eq = pub.equipment
        href = (pub.url or "").strip()
        if not href and pub.doi:
            href = f"https://doi.org/{str(pub.doi).strip()}"
        results.append(
            {
                "id": pub.equipment_publication_id,
                "title": pub.title,
                "citation": pub.citation or "",
                "url": href,
                "doi": pub.doi or "",
                "year": pub.year,
                "equipment_id": eq.equipment_id if eq else None,
                "equipment_name": eq.name if eq else "",
                "equipment_code": eq.code if eq else "",
            }
        )
    return Response({"count": qs.count(), "results": results})


@api_view(["GET"])
@permission_classes([AllowAny])
def hero_slides(request):
    """Return active hero background images for the carousel (order, image_url, alt_text)."""
    slides = HeroSlide.objects.filter(is_active=True).order_by("order", "id")
    base = request.build_absolute_uri("/").rstrip("/")
    result = []
    for slide in slides:
        if slide.image:
            url = slide.image.url
            if url.startswith("/"):
                url = base + url
            result.append({
                "id": slide.id,
                "order": slide.order,
                "image_url": url,
                "alt_text": slide.alt_text or "",
            })
    return Response(result)


@api_view(["GET"])
@permission_classes([AllowAny])
def analysis_charges_document(request):
    """Return the public Analysis Charges PDF URL, or 404 if not uploaded yet."""
    from .serializers import SiteDocumentSerializer

    try:
        doc = SiteDocument.objects.get(key=SiteDocument.Key.ANALYSIS_CHARGES)
    except SiteDocument.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if not doc.document:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = SiteDocumentSerializer(doc, context={"request": request})
    return Response(
        {
            "key": doc.key,
            "title": doc.title or "Analysis Charges",
            "document_url": serializer.data.get("document_url"),
            "updated_at": serializer.data.get("updated_at"),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def page_by_slug(request, slug):
    """Return a published CMS page by slug. 404 if not found or not published."""
    from .serializers import CmsPagePublicSerializer

    try:
        page = CmsPage.objects.get(slug=slug, is_published=True)
    except CmsPage.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = CmsPagePublicSerializer(page)
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def policy_pages(request):
    """The website policy pages that are published (for the footer), in a fixed order: [{slug, title}]."""
    from .policy_pages import POLICY_SLUGS

    published = {p.slug: p.title for p in CmsPage.objects.filter(slug__in=POLICY_SLUGS, is_published=True)}
    return Response({"pages": [{"slug": s, "title": published[s]} for s in POLICY_SLUGS if s in published]})


@api_view(["GET"])
@permission_classes([AllowAny])
def hall_of_frames(request):
    """Researchers with approved publications, most publications first (worked out automatically; researchers the
    Main Administrator took off are left out). Empty until the Main Administrator switches the Hall of Frames on."""
    from .recognition import hall_enabled, hall_of_frames as build

    if not hall_enabled():
        return Response({"results": [], "enabled": False})
    return Response({"results": build(request=request), "enabled": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def acknowledgers(request):
    """Everyone whose publication acknowledging IIC has been approved, for the scrolling list in the home page hero
    (most publications first). Independent of the Hall of Frames switch; researchers the Main Administrator took off
    the home page are left out."""
    from .recognition import hall_of_frames as build

    return Response({"results": build(request=request, limit=ACKNOWLEDGERS_LIMIT)})


ACKNOWLEDGERS_LIMIT = 200


@api_view(["GET"])
@permission_classes([AllowAny])
def researcher_of_the_month(request):
    """This month's Researchers of the Month (institute-wide and per department), chosen automatically from approved
    publications on the first request of the month. ``?department=<id>`` narrows to one department; ``?history=1``
    lists earlier months instead. This month's list has one card per researcher, with all their ``titles``."""
    from django.utils import timezone

    from .models import ResearcherOfTheMonth
    from .recognition import ensure_month, month_card, one_card_per_person

    this_month = timezone.localdate().replace(day=1)
    ensure_month(this_month)
    qs = ResearcherOfTheMonth.objects.filter(is_approved=True).select_related("department", "user")
    dept = request.query_params.get("department")
    if dept and str(dept).isdigit():
        qs = qs.filter(department_id=int(dept))
    history = str(request.query_params.get("history", "")).lower() in ("1", "true", "yes")
    qs = qs.filter(month__lt=this_month)[:36] if history else qs.filter(month=this_month)
    cards = [month_card(r, request=request) for r in qs]
    return Response({"month": this_month.isoformat(), "history": history,
                     "results": cards if history else one_card_per_person(cards)})


def _main_admin_only(request):
    from iic_booking.department_modules.access import is_main_admin

    if not is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can change what the home page shows."},
                        status=status.HTTP_403_FORBIDDEN)
    return None


@api_view(["GET"])
def recognition_admin(request):
    """Admin Panel users: the Hall of Frames (with who is hidden), this month's Researchers of the Month (shown or
    not) and the last twelve months."""
    from django.utils import timezone

    from iic_booking.users.rbac import user_has_admin_panel_access

    from .models import ResearcherOfTheMonth
    from .recognition import ensure_month, hall_enabled, hall_of_frames as build, month_card

    if not user_has_admin_panel_access(request.user):
        return Response({"error": "Admin panel access required."}, status=status.HTTP_403_FORBIDDEN)
    this_month = timezone.localdate().replace(day=1)
    ensure_month(this_month)
    rows = ResearcherOfTheMonth.objects.select_related("department", "user")
    return Response({
        "hall_enabled": hall_enabled(),
        "hall": build(request=request, include_hidden=True, limit=200),
        "month": this_month.isoformat(),
        "current": [month_card(r, request=request) for r in rows.filter(month=this_month)],
        "history": [month_card(r, request=request) for r in rows.filter(month__lt=this_month)[:60]],
    })


@api_view(["POST"])
def recognition_hall_enabled(request):
    """Main Administrator: ``{"enabled": true|false}`` shows or hides the whole Hall of Frames on the home page."""
    from .recognition import hall_enabled, set_hall_enabled

    refused = _main_admin_only(request)
    if refused:
        return refused
    set_hall_enabled(request.data.get("enabled") in (True, "true", "1", 1))
    return Response({"enabled": hall_enabled()})


@api_view(["POST"])
def recognition_hall_visibility(request, user_id):
    """Main Administrator: ``{"hidden": true|false, "reason"}`` takes a researcher off the Hall of Frames, or back."""
    from .recognition import hide_from_hall, show_in_hall

    refused = _main_admin_only(request)
    if refused:
        return refused
    if request.data.get("hidden") in (False, "false", "0", 0):
        show_in_hall(int(user_id))
    else:
        hide_from_hall(int(user_id), actor=request.user, reason=str(request.data.get("reason") or ""))
    return Response({"ok": True})


@api_view(["POST"])
def recognition_month_disable(request, row_id):
    """Main Administrator: take this month's researcher off; the next eligible researcher is chosen automatically."""
    from .models import ResearcherOfTheMonth
    from .recognition import disable_month, month_card

    refused = _main_admin_only(request)
    if refused:
        return refused
    if not ResearcherOfTheMonth.objects.filter(pk=row_id).exists():
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    row = disable_month(int(row_id), actor=request.user, reason=str(request.data.get("reason") or ""))
    return Response(month_card(row, request=request))
