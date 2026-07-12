"""Public (read-only) API views for CMS."""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from .models import MenuItem, HomePageContent, HeroSlide, CmsPage


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
    Public counts for the home hero: operational equipment visible on the public catalog,
    active user accounts (is_active=True), and total bookings placed on the portal.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import AnonymousUser

    from iic_booking.equipment.api_views import get_visible_equipment_queryset
    from iic_booking.equipment.models import Booking, EquipmentStatus

    User = get_user_model()
    vis_user = (
        request.user
        if getattr(request.user, "is_authenticated", False)
        else AnonymousUser()
    )
    equipment_count = (
        get_visible_equipment_queryset(vis_user)
        .filter(status=EquipmentStatus.ACTIVE)
        .count()
    )
    active_users_count = User.objects.filter(is_active=True).count()
    total_bookings_count = Booking.objects.count()
    return Response(
        {
            "equipment_count": equipment_count,
            "active_users_count": active_users_count,
            "total_bookings_count": total_bookings_count,
        }
    )


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
def page_by_slug(request, slug):
    """Return a published CMS page by slug. 404 if not found or not published."""
    from .serializers import CmsPagePublicSerializer

    try:
        page = CmsPage.objects.get(slug=slug, is_published=True)
    except CmsPage.DoesNotExist:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    serializer = CmsPagePublicSerializer(page)
    return Response(serializer.data)
