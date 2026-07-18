"""Portal UX feedback API (user submit/update + admin list/stats)."""

from django.db.models import Avg, Count, Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import UserType
from .models import PortalFeedback
from .serializers import PortalFeedbackSerializer


def _is_admin(user) -> bool:
    return bool(user and getattr(user, "user_type", None) == UserType.ADMIN)


def _validate_star(value, field: str):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer from 1 to 5")
    if n < 1 or n > 5:
        raise ValueError(f"{field} must be between 1 and 5")
    return n


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def portal_feedback_mine(request):
    """Get or upsert the current user's portal feedback."""
    if request.method == "GET":
        fb = PortalFeedback.objects.filter(user=request.user).first()
        if not fb:
            return Response({"feedback": None})
        return Response({"feedback": PortalFeedbackSerializer(fb).data})

    data = request.data or {}
    try:
        payload = {
            "overall_rating": _validate_star(data.get("overall_rating"), "overall_rating"),
            "ease_of_booking": _validate_star(data.get("ease_of_booking"), "ease_of_booking"),
            "website_usability": _validate_star(data.get("website_usability"), "website_usability"),
            "equipment_booking_experience": _validate_star(
                data.get("equipment_booking_experience"), "equipment_booking_experience"
            ),
            "suggestions": (data.get("suggestions") or "")[:5000],
            "comments": (data.get("comments") or "")[:5000],
        }
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    fb, created = PortalFeedback.objects.update_or_create(
        user=request.user,
        defaults=payload,
    )
    return Response(
        {"feedback": PortalFeedbackSerializer(fb).data, "created": created},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_feedback_admin_list(request):
    """Admin: list feedback with filters + summary stats."""
    if not _is_admin(request.user):
        return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

    qs = PortalFeedback.objects.select_related("user", "user__department").all()

    user_type = (request.query_params.get("user_type") or "").strip()
    if user_type:
        qs = qs.filter(user__user_type__iexact=user_type)

    department_id = request.query_params.get("department_id")
    if department_id:
        try:
            qs = qs.filter(user__department_id=int(department_id))
        except (TypeError, ValueError):
            return Response({"error": "Invalid department_id"}, status=status.HTTP_400_BAD_REQUEST)

    min_rating = request.query_params.get("min_rating")
    if min_rating:
        try:
            qs = qs.filter(overall_rating__gte=int(min_rating))
        except (TypeError, ValueError):
            return Response({"error": "Invalid min_rating"}, status=status.HTTP_400_BAD_REQUEST)

    max_rating = request.query_params.get("max_rating")
    if max_rating:
        try:
            qs = qs.filter(overall_rating__lte=int(max_rating))
        except (TypeError, ValueError):
            return Response({"error": "Invalid max_rating"}, status=status.HTTP_400_BAD_REQUEST)

    date_from = request.query_params.get("date_from")
    if date_from:
        d = parse_date(date_from)
        if d:
            qs = qs.filter(updated_at__date__gte=d)

    date_to = request.query_params.get("date_to")
    if date_to:
        d = parse_date(date_to)
        if d:
            qs = qs.filter(updated_at__date__lte=d)

    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(user__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(suggestions__icontains=search)
            | Q(comments__icontains=search)
        )

    try:
        limit = min(int(request.query_params.get("limit") or 50), 200)
        offset = max(int(request.query_params.get("offset") or 0), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    total = qs.count()
    page = qs[offset : offset + limit]

    agg = qs.aggregate(
        avg_overall=Avg("overall_rating"),
        avg_ease=Avg("ease_of_booking"),
        avg_usability=Avg("website_usability"),
        avg_equipment=Avg("equipment_booking_experience"),
        count=Count("feedback_id"),
    )

    rating_dist = {
        str(r): qs.filter(overall_rating=r).count() for r in range(1, 6)
    }

    by_user_type = list(
        qs.values("user__user_type")
        .annotate(count=Count("feedback_id"), avg_overall=Avg("overall_rating"))
        .order_by("-count")
    )

    return Response(
        {
            "count": total,
            "feedback": PortalFeedbackSerializer(page, many=True).data,
            "stats": {
                "total": agg["count"] or 0,
                "avg_overall": round(agg["avg_overall"] or 0, 2),
                "avg_ease_of_booking": round(agg["avg_ease"] or 0, 2),
                "avg_website_usability": round(agg["avg_usability"] or 0, 2),
                "avg_equipment_booking_experience": round(agg["avg_equipment"] or 0, 2),
                "rating_distribution": rating_dist,
                "by_user_type": [
                    {
                        "user_type": row["user__user_type"],
                        "count": row["count"],
                        "avg_overall": round(row["avg_overall"] or 0, 2),
                    }
                    for row in by_user_type
                ],
            },
        }
    )
