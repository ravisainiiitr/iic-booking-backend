"""API views for notifications and notices."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
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
    notifications = CommunicationLog.objects.filter(
        recipient=request.user,
        communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
    ).order_by("-created_at")
    
    # Transform to API format
    notification_list = []
    for notification in notifications:
        notification_list.append({
            "id": notification.id,
            "title": notification.subject or "Notification",
            "message": notification.message or "",
            "type": notification.metadata.get("notification_type", "info") if notification.metadata else "info",
            "read": notification.status == CommunicationLog.CommunicationStatus.READ,
            "created_at": notification.created_at.isoformat() if notification.created_at else None,
            "link": notification.metadata.get("link") if notification.metadata else None,
        })
    
    return Response(notification_list, status=status.HTTP_200_OK)


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
        return Response(NoticeSerializer(notice).data, status=status.HTTP_201_CREATED)

    # GET
    from iic_booking.equipment.models import EquipmentTemporaryOIC
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic

    managed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
    temp_ids = set(
        EquipmentTemporaryOIC.objects.filter(
            temporary_oic=request.user,
            resume_at__gt=timezone.now(),
        ).values_list("equipment_id", flat=True)
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
    """OIC sets expiry (or unlimited) on an equipment DRAFT and submits for approval."""
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

    notice.expiry_unlimited = unlimited
    notice.expiry_date = None if unlimited else expiry_date
    notice.needs_oic_expiry = False
    notice.approval_status = Notice.ApprovalStatus.PENDING
    notice.is_active = False
    if not notice.requested_by_id:
        notice.requested_by = request.user
    notice.save()
    return Response(NoticeSerializer(notice).data, status=status.HTTP_200_OK)


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
    return Response(NoticeSerializer(notice).data, status=status.HTTP_200_OK)


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
