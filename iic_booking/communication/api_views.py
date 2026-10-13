"""API views for notifications and notices."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import CommunicationLog, Notice
from .serializers import NoticeSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_notifications(request):
    """
    Get all push notifications for the current user.
    
    Returns a list of push notifications (CommunicationLog entries with type PUSH_NOTIFICATION).
    """
    notifications = (
        CommunicationLog.objects.filter(
            recipient=request.user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        )
        .select_related("template")
        .order_by("-created_at")
    )
    return Response([_notification_row(n) for n in notifications], status=status.HTTP_200_OK)


def _notification_row(notification) -> dict:
    from .notification_preferences import category_for

    meta = notification.metadata or {}
    template_code = notification.template.code if notification.template_id else None
    return {
        "id": notification.id,
        "title": notification.subject or "Notification",
        "message": notification.message or "",
        "type": meta.get("notification_type", "info"),
        "read": notification.status == CommunicationLog.CommunicationStatus.READ,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "link": meta.get("link"),
        "category": category_for(template_code, meta, notification.subject or ""),
    }


INBOX_SCAN_LIMIT = 2000


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def notification_inbox(request):
    """Paged notification centre: ?category=&unread=1&q=&page=&page_size=."""
    qs = (
        CommunicationLog.objects.filter(
            recipient=request.user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        )
        .select_related("template")
        .order_by("-created_at")
    )
    q = (request.query_params.get("q") or "").strip()[:100]
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(message__icontains=q))
    if request.query_params.get("unread") in ("1", "true", "yes"):
        qs = qs.exclude(status=CommunicationLog.CommunicationStatus.READ)

    rows = [_notification_row(n) for n in qs[:INBOX_SCAN_LIMIT]]
    category_counts: dict[str, int] = {}
    for row in rows:
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
    category = (request.query_params.get("category") or "").strip()
    if category and category != "all":
        rows = [r for r in rows if r["category"] == category]

    try:
        page = max(1, int(request.query_params.get("page") or 1))
        page_size = min(100, max(5, int(request.query_params.get("page_size") or 20)))
    except ValueError:
        return Response({"error": "page and page_size must be numbers"}, status=status.HTTP_400_BAD_REQUEST)
    start = (page - 1) * page_size
    unread_count = (
        CommunicationLog.objects.filter(
            recipient=request.user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        )
        .exclude(status=CommunicationLog.CommunicationStatus.READ)
        .count()
    )
    return Response(
        {
            "results": rows[start : start + page_size],
            "count": len(rows),
            "page": page,
            "page_size": page_size,
            "unread_count": unread_count,
            "category_counts": category_counts,
        }
    )


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def notification_preferences_view(request):
    """GET the category x channel matrix; PUT {"categories": [{key, email, in_app, digest}], "delivery": {...}}."""
    from django.db import transaction

    from .notification_preferences import preferences_payload, save_preferences

    if request.method == "PUT":
        data = request.data if isinstance(request.data, dict) else {}
        try:
            with transaction.atomic():
                save_preferences(request.user, data.get("categories"), data.get("delivery"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(preferences_payload(request.user))


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def web_push_subscriptions(request):
    """Browser push: GET the server key; POST a PushSubscription JSON; DELETE {"endpoint"} to stop."""
    from .models import WebPushSubscription
    from .web_push import vapid_public_key

    data = request.data if isinstance(request.data, dict) else {}
    if request.method == "POST":
        endpoint = str(data.get("endpoint") or "").strip()
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        p256dh, auth = str(keys.get("p256dh") or "").strip(), str(keys.get("auth") or "").strip()
        if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth:
            return Response({"error": "Invalid push subscription."}, status=status.HTTP_400_BAD_REQUEST)
        WebPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": request.user,
                "p256dh": p256dh[:200],
                "auth": auth[:100],
                "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:255],
                "failure_count": 0,
            },
        )
    elif request.method == "DELETE":
        endpoint = str(data.get("endpoint") or "").strip()
        if endpoint:
            WebPushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return Response(
        {
            "public_key": vapid_public_key(),
            "subscriptions": WebPushSubscription.objects.filter(user=request.user).count(),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def web_push_test(request):
    """Send a test push to the caller's browsers."""
    from .models import WebPushSubscription
    from .web_push import send_to_subscription
    import json

    payload = json.dumps(
        {
            "title": "Browser notifications are on",
            "body": "You will see portal updates here even when the IIC booking portal is closed.",
            "link": "/notifications?tab=settings",
            "tag": "push-test",
        }
    ).encode("utf-8")
    sent = sum(
        int(send_to_subscription(sub, payload)) for sub in WebPushSubscription.objects.filter(user=request.user)
    )
    if not sent:
        return Response(
            {"error": "No browser accepted the test. Turn browser notifications off and on again."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({"sent": sent})


@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def notification_unsubscribe(request, token):
    """One-click unsubscribe from the List-Unsubscribe email header (RFC 8058).

    POST switches the category's emails off. GET never changes anything (link scanners follow GETs): it sends
    people to the portal's confirmation page, or with ``?info=1`` describes the token for that page.
    """
    from django.conf import settings as django_settings
    from django.http import HttpResponseRedirect

    from .notification_preferences import read_unsubscribe_token, unsubscribe

    found = read_unsubscribe_token(token)
    if request.method == "GET" and request.query_params.get("info") != "1":
        base = (getattr(django_settings, "FRONTEND_URL", "") or "").rstrip("/")
        return HttpResponseRedirect(f"{base}/unsubscribe?t={token}")
    if found is None:
        return Response(
            {"error": "This unsubscribe link is not valid. Manage your emails from Notification settings."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    user, cat = found
    if request.method == "POST":
        unsubscribe(user, cat.key)
    from .models import NotificationPreference

    pref = NotificationPreference.objects.filter(user=user, category=cat.key).first()
    return Response(
        {
            "category": cat.key,
            "label": cat.label,
            "description": cat.description,
            "email_enabled": pref.email_enabled if pref else True,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_notification_as_read(request, notification_id):
    """
    Mark a specific notification as read.
    
    Args:
        notification_id: ID of the notification to mark as read
    """
    try:
        notification = CommunicationLog.objects.get(
            id=notification_id,
            recipient=request.user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        )
        
        # Update status to READ
        from django.utils import timezone
        notification.status = CommunicationLog.CommunicationStatus.READ
        notification.read_at = timezone.now()
        notification.save(update_fields=["status", "read_at"])
        
        return Response(
            {"message": "Notification marked as read", "id": notification.id},
            status=status.HTTP_200_OK,
        )
    except CommunicationLog.DoesNotExist:
        return Response(
            {"error": "Notification not found"},
            status=status.HTTP_404_NOT_FOUND,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_all_notifications_as_read(request):
    """
    Mark all notifications for the current user as read.
    """
    from django.utils import timezone
    
    notifications = CommunicationLog.objects.filter(
        recipient=request.user,
        communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        status__in=[
            CommunicationLog.CommunicationStatus.PENDING,
            CommunicationLog.CommunicationStatus.SENT,
            CommunicationLog.CommunicationStatus.DELIVERED,
        ],
    )
    
    count = notifications.update(
        status=CommunicationLog.CommunicationStatus.READ,
        read_at=timezone.now(),
    )
    
    return Response(
        {"message": f"{count} notifications marked as read", "count": count},
        status=status.HTTP_200_OK,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_notification(request, notification_id):
    """
    Delete a specific notification.
    
    Args:
        notification_id: ID of the notification to delete
    """
    try:
        notification = CommunicationLog.objects.get(
            id=notification_id,
            recipient=request.user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        )
        
        notification.delete()
        
        return Response(
            {"message": "Notification deleted", "id": notification_id},
            status=status.HTTP_200_OK,
        )
    except CommunicationLog.DoesNotExist:
        return Response(
            {"error": "Notification not found"},
            status=status.HTTP_404_NOT_FOUND,
        )


# Notice Board API Views

@api_view(["GET", "POST"])
@permission_classes([AllowAny])  # Allow public access for GET, check auth manually for POST
def notice_list(request):
    """
    Get list of active public notices (GET) or create a new notice (POST).

    GET: Public access — APPROVED + active + not expired
    POST: Requires authentication and admin privileges (publishes as APPROVED)
    """
    if request.method == "GET":
        from .notice_board_service import public_notices_queryset

        queryset = public_notices_queryset()

        # Optional override for admin tooling: ?is_active=... still only APPROVED public board
        is_active_param = request.query_params.get("is_active")
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1", "yes")
            queryset = queryset.filter(is_active=is_active) if is_active else Notice.objects.none()

        notice_type = request.query_params.get("notice_type")
        if notice_type:
            queryset = queryset.filter(notice_type=notice_type)

        queryset = queryset.order_by("-priority", "-created_at")

        limit = request.query_params.get("limit")
        if limit:
            try:
                queryset = queryset[: int(limit)]
            except ValueError:
                pass

        serializer = NoticeSerializer(queryset, many=True)
        return Response(
            {
                "notices": serializer.data,
                "count": len(serializer.data),
            },
            status=status.HTTP_200_OK,
        )

    elif request.method == "POST":
        if not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        from .notice_board_service import is_main_admin

        if not is_main_admin(request.user) and not request.user.is_staff:
            return Response(
                {"error": "Only admins can create notices."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = NoticeSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(
                created_by=request.user,
                requested_by=request.user,
                approval_status=Notice.ApprovalStatus.APPROVED,
                is_active=request.data.get("is_active", True),
                source=Notice.Source.MANUAL,
                needs_oic_expiry=False,
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "PATCH", "PUT", "DELETE"])
@permission_classes([AllowAny])  # Allow public access, but we check auth manually for write operations
def notice_detail(request, notice_id):
    """
    Get, update, or delete a notice.

    GET: Public access for APPROVED notices (others require auth + permission)
    PATCH/PUT/DELETE: Requires authentication and admin privileges
    """
    try:
        notice = Notice.objects.select_related(
            "created_by", "requested_by", "reviewed_by", "equipment"
        ).get(notice_id=notice_id)
    except Notice.DoesNotExist:
        return Response(
            {"error": "Notice not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        from .notice_board_service import is_main_admin, user_can_manage_notice_request

        if notice.approval_status != Notice.ApprovalStatus.APPROVED or not notice.is_active:
            if not request.user.is_authenticated or (
                not is_main_admin(request.user)
                and not user_can_manage_notice_request(request.user, notice)
            ):
                return Response(
                    {"error": "Notice not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        serializer = NoticeSerializer(notice)
        return Response(serializer.data)

    if not request.user.is_authenticated:
        return Response(
            {"error": "Authentication required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    from .notice_board_service import is_main_admin

    if not is_main_admin(request.user) and not request.user.is_staff:
        return Response(
            {"error": "Only admins can update or delete notices."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "DELETE":
        notice.delete()
        return Response(
            {"message": "Notice deleted successfully."},
            status=status.HTTP_200_OK,
        )

    serializer = NoticeSerializer(notice, data=request.data, partial=(request.method == "PATCH"))
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "y")


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def notice_requests_mine(request):
    """
    OIC: list my notice requests / equipment drafts I can complete.
    POST: create a generic notice request (PENDING approval).
    """
    from .notice_board_service import is_main_admin, is_oic

    if not (is_oic(request.user) or is_main_admin(request.user)):
        return Response(
            {"error": "Only Officer In Charge can manage notice board requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "POST":
        title = (request.data.get("title") or "").strip()
        description = (request.data.get("description") or "").strip()
        if not title or not description:
            return Response(
                {"error": "title and description are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        notice_type = (request.data.get("notice_type") or Notice.NoticeType.INFO).strip()
        if notice_type not in dict(Notice.NoticeType.choices):
            notice_type = Notice.NoticeType.INFO
        try:
            priority = int(request.data.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        unlimited = _parse_bool(request.data.get("expiry_unlimited"))
        expiry_raw = request.data.get("expiry_date")
        expiry_date = None
        if not unlimited and expiry_raw:
            from django.utils.dateparse import parse_datetime

            expiry_date = parse_datetime(str(expiry_raw))
            if expiry_date is None:
                return Response(
                    {"error": "Invalid expiry_date. Use ISO datetime."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if timezone.is_naive(expiry_date):
                expiry_date = timezone.make_aware(expiry_date, timezone.get_current_timezone())

        notice = Notice.objects.create(
            title=title,
            description=description,
            content=(request.data.get("content") or "").strip() or None,
            notice_type=notice_type,
            is_active=False,
            priority=priority,
            created_by=request.user,
            requested_by=request.user,
            expiry_date=None if unlimited else expiry_date,
            expiry_unlimited=unlimited,
            needs_oic_expiry=False,
            approval_status=Notice.ApprovalStatus.PENDING,
            source=Notice.Source.MANUAL,
        )
        _notify_notice_submitted(notice, request.user)
        return Response(NoticeSerializer(notice).data, status=status.HTTP_201_CREATED)

    # GET
    from iic_booking.equipment.models import EquipmentTemporaryOIC
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

    managed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
    temp_ids = set(
        EquipmentTemporaryOIC.objects.active()
        .filter(temporary_oic=request.user)
        .values_list("equipment_id", flat=True)
    )
    equipment_ids = managed_ids | temp_ids

    qs = (
        Notice.objects.filter(
            Q(requested_by=request.user)
            | Q(created_by=request.user)
            | Q(
                equipment_id__in=equipment_ids,
                source=Notice.Source.EQUIPMENT_UNAVAILABLE,
                approval_status__in=[
                    Notice.ApprovalStatus.DRAFT,
                    Notice.ApprovalStatus.PENDING,
                    Notice.ApprovalStatus.APPROVED,
                    Notice.ApprovalStatus.REJECTED,
                ],
            )
        )
        .select_related("equipment", "requested_by", "reviewed_by", "created_by")
        .distinct()
        .order_by("-updated_at")
    )
    needs_expiry = qs.filter(
        needs_oic_expiry=True,
        approval_status=Notice.ApprovalStatus.DRAFT,
        source=Notice.Source.EQUIPMENT_UNAVAILABLE,
    )
    serializer = NoticeSerializer(qs[:200], many=True)
    needs_ser = NoticeSerializer(needs_expiry[:50], many=True)
    return Response(
        {
            "requests": serializer.data,
            "needs_expiry": needs_ser.data,
            "count": len(serializer.data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def notice_request_complete_expiry(request, notice_id):
    """OIC sets expiry (or unlimited) on an equipment DRAFT; the equipment-unavailability notice is published at once.

    Main Admin approval applies to generic notice requests only.
    """
    from .notice_board_service import is_oic, user_can_manage_notice_request

    if not is_oic(request.user):
        from .notice_board_service import is_main_admin

        if not is_main_admin(request.user):
            return Response(
                {"error": "Only Officer In Charge can complete notice expiry."},
                status=status.HTTP_403_FORBIDDEN,
            )

    try:
        notice = Notice.objects.select_related("equipment").get(notice_id=notice_id)
    except Notice.DoesNotExist:
        return Response({"error": "Notice request not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_manage_notice_request(request.user, notice):
        return Response({"error": "Not allowed for this notice."}, status=status.HTTP_403_FORBIDDEN)

    if notice.approval_status != Notice.ApprovalStatus.DRAFT and not notice.needs_oic_expiry:
        return Response(
            {"error": "This notice does not need expiry completion."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    unlimited = _parse_bool(request.data.get("expiry_unlimited"))
    expiry_date = None
    if not unlimited:
        expiry_raw = request.data.get("expiry_date")
        if not expiry_raw:
            return Response(
                {"error": "Provide expiry_date or set expiry_unlimited=true."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from django.utils.dateparse import parse_datetime

        expiry_date = parse_datetime(str(expiry_raw))
        if expiry_date is None:
            return Response(
                {"error": "Invalid expiry_date. Use ISO datetime."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timezone.is_naive(expiry_date):
            expiry_date = timezone.make_aware(expiry_date, timezone.get_current_timezone())
        if expiry_date <= timezone.now():
            return Response(
                {"error": "Choose an expiry in the future, or Unlimited."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    notice.expiry_unlimited = unlimited
    notice.expiry_date = None if unlimited else expiry_date
    notice.needs_oic_expiry = False
    if not notice.requested_by_id:
        notice.requested_by = request.user
    if notice.source == Notice.Source.EQUIPMENT_UNAVAILABLE:
        from .notice_board_service import publish_equipment_notice

        publish_equipment_notice(notice, actor=request.user)
        _notify_equipment_notice_published(notice, request.user)
    else:
        notice.approval_status = Notice.ApprovalStatus.PENDING
        notice.is_active = False
        notice.save()
        _notify_notice_submitted(notice, request.user)
    return Response(NoticeSerializer(notice).data, status=status.HTTP_200_OK)


def _notify_equipment_notice_published(notice, actor) -> None:
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    from .in_app import notify_in_app, person_label

    title = (getattr(notice, "title", "") or "").strip() or f"Notice #{notice.notice_id}"
    extra = {"notice_id": notice.notice_id}
    notify_in_app(
        [actor],
        title="Notice published",
        message=f"“{title}” is now on the notice board.",
        link="/notice-board-requests",
        event="notice.published",
        created_by=actor,
        extra=extra,
    )
    admins = User.objects.filter(user_type=UserType.ADMIN, is_active=True).exclude(pk=actor.pk)[:50]
    notify_in_app(
        admins,
        title="Equipment notice published",
        message=f"{person_label(actor)} published “{title}” on the notice board. No approval is needed.",
        link="/admin-settings/communication?tab=notices",
        event="notice.published",
        created_by=actor,
        extra=extra,
    )


def _notify_notice_submitted(notice, actor) -> None:
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    from .in_app import notify_in_app, person_label

    title = (getattr(notice, "title", "") or "").strip() or f"Notice #{notice.notice_id}"
    extra = {"notice_id": notice.notice_id}
    notify_in_app(
        [actor],
        title="Notice request submitted",
        message=f"“{title}” was sent to the Main Admin for approval.",
        link="/notice-board-requests",
        event="notice.submitted",
        created_by=actor,
        extra=extra,
    )
    admins = User.objects.filter(user_type=UserType.ADMIN, is_active=True).exclude(pk=actor.pk)[:50]
    notify_in_app(
        admins,
        title="Action needed: notice board request",
        message=f"{person_label(actor)} requested the notice “{title}”. Approve or reject it from Communication → Notices.",
        link="/admin-settings/communication?tab=notices",
        notification_type="warning",
        event="notice.submitted",
        created_by=actor,
        extra={**extra, "action_required": True},
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def notice_requests_pending(request):
    """Main Admin: list PENDING notice requests awaiting approval."""
    from .notice_board_service import is_main_admin

    if not is_main_admin(request.user):
        return Response(
            {"error": "Only Main Admin can view pending notice approvals."},
            status=status.HTTP_403_FORBIDDEN,
        )
    qs = (
        Notice.objects.filter(approval_status=Notice.ApprovalStatus.PENDING)
        .select_related("equipment", "requested_by", "created_by")
        .order_by("-created_at")
    )
    return Response(
        {"requests": NoticeSerializer(qs, many=True).data, "count": qs.count()},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def notice_request_approve(request, notice_id):
    from .notice_board_service import is_main_admin

    if not is_main_admin(request.user):
        return Response(
            {"error": "Only Main Admin can approve notice requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        notice = Notice.objects.get(notice_id=notice_id)
    except Notice.DoesNotExist:
        return Response({"error": "Notice request not found."}, status=status.HTTP_404_NOT_FOUND)
    if notice.approval_status != Notice.ApprovalStatus.PENDING:
        return Response(
            {"error": f"Notice is {notice.approval_status}, not PENDING."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if notice.needs_oic_expiry:
        return Response(
            {"error": "OIC must set expiry before this notice can be approved."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    notice.approval_status = Notice.ApprovalStatus.APPROVED
    notice.is_active = True
    notice.reviewed_by = request.user
    notice.reviewed_at = timezone.now()
    notice.review_comment = (request.data.get("review_comment") or "").strip()
    notice.save()
    _notify_notice_decided(notice, request.user)
    return Response(NoticeSerializer(notice).data, status=status.HTTP_200_OK)


def _notify_notice_decided(notice, actor) -> None:
    from .in_app import notify_in_app, person_label

    approved = notice.approval_status == Notice.ApprovalStatus.APPROVED
    verb = "approved" if approved else "rejected"
    title = (getattr(notice, "title", "") or "").strip() or f"Notice #{notice.notice_id}"
    comment = (notice.review_comment or "").strip()
    requester = notice.requested_by or getattr(notice, "created_by", None)
    extra = {"notice_id": notice.notice_id}
    if requester is not None and requester.id != actor.id:
        notify_in_app(
            [requester],
            title=f"Notice request {verb}",
            message=f"“{title}” was {verb} by {person_label(actor)}." + (f" Comment: {comment}" if comment else ""),
            link="/notice-board-requests",
            notification_type="info" if approved else "warning",
            event=f"notice.{verb}",
            created_by=actor,
            extra=extra,
        )
    notify_in_app(
        [actor],
        title=f"You {verb} a notice request",
        message=f"“{title}”" + (f" from {person_label(requester)}." if requester else "."),
        link="/admin-settings/communication?tab=notices",
        event=f"notice.{verb}",
        created_by=actor,
        extra=extra,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def notice_request_reject(request, notice_id):
    from .notice_board_service import is_main_admin

    if not is_main_admin(request.user):
        return Response(
            {"error": "Only Main Admin can reject notice requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        notice = Notice.objects.get(notice_id=notice_id)
    except Notice.DoesNotExist:
        return Response({"error": "Notice request not found."}, status=status.HTTP_404_NOT_FOUND)
    if notice.approval_status != Notice.ApprovalStatus.PENDING:
        return Response(
            {"error": f"Notice is {notice.approval_status}, not PENDING."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    notice.approval_status = Notice.ApprovalStatus.REJECTED
    notice.is_active = False
    notice.reviewed_by = request.user
    notice.reviewed_at = timezone.now()
    notice.review_comment = (request.data.get("review_comment") or "").strip()
    notice.needs_oic_expiry = False
    notice.save()
    _notify_notice_decided(notice, request.user)
    return Response(NoticeSerializer(notice).data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_inbox_folders(request):

    from iic_booking.users.models.user_type import UserType

    if not (
        getattr(request.user, "is_staff", False)
        or getattr(request.user, "user_type", None) == UserType.ADMIN
    ):
        return Response({"error": "Only admin can list inbox folders."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from .imap_service import get_imap_reader
        reader = get_imap_reader()
        with reader:
            folders = reader.list_folders_with_counts()
        return Response({"folders": folders}, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": f"IMAP error: {str(e)}"}, status=status.HTTP_502_BAD_GATEWAY)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def fetch_inbox_emails(request):
    """
    Fetch emails from the configured IMAP inbox (Main Admin / staff only).
    
    Query params:
        mailbox: Folder name (default INBOX).
        max_count: Max messages to return (default 50).
        since: IMAP date e.g. 01-Jan-2025 (optional).
    
    Returns:
        { "emails": [ { uid, subject, from, date, date_raw, body_plain, body_html } ], "count": N }
    """
    if not request.user.is_authenticated:
        return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
    from iic_booking.users.models.user_type import UserType

    if not (
        getattr(request.user, "is_staff", False)
        or getattr(request.user, "user_type", None) == UserType.ADMIN
    ):
        return Response({"error": "Only admin can fetch inbox emails."}, status=status.HTTP_403_FORBIDDEN)

    mailbox = request.query_params.get("mailbox") or "INBOX"
    try:
        max_count = int(request.query_params.get("max_count", 50))
    except ValueError:
        max_count = 50
    max_count = min(max(max_count, 1), 200)
    since = request.query_params.get("since") or None

    try:
        from .imap_service import get_imap_reader
        reader = get_imap_reader(mailbox=mailbox)
        with reader:
            emails, mailbox_total = reader.fetch_emails(
                mailbox=mailbox, since=since, max_count=max_count, mark_seen=False
            )
        # Serialize date to ISO string for JSON
        for em in emails:
            if em.get("date"):
                em["date"] = em["date"].isoformat()
        return Response(
            {"emails": emails, "count": len(emails), "mailbox_total": mailbox_total},
            status=status.HTTP_200_OK,
        )
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": f"IMAP error: {str(e)}"}, status=status.HTTP_502_BAD_GATEWAY)
