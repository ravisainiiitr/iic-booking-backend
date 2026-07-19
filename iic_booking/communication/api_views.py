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
    
    GET: Public access, no authentication required
    POST: Requires authentication and admin privileges
    
    Query Parameters (GET):
        - limit: Maximum number of notices to return (default: all)
        - notice_type: Filter by notice type (info, warning, urgent)
        - is_active: Filter by active status (default: true)
    
    Request Body (POST):
        {
            "title": "Notice title",
            "description": "Short description",
            "content": "Full content (optional)",
            "notice_type": "info|warning|urgent",
            "is_active": true,
            "priority": 0
        }
    
    Returns:
        GET: List of active notices
        POST: Created notice
    """
    if request.method == "GET":
        # Public access for GET - start with all notices
        queryset = Notice.objects.all()
        
        # Filter by is_active if provided, otherwise default to active only
        is_active_param = request.query_params.get('is_active')
        if is_active_param is not None:
            # Convert string to boolean
            is_active = is_active_param.lower() in ('true', '1', 'yes')
            queryset = queryset.filter(is_active=is_active)
        else:
            # Default to active notices only
            queryset = queryset.filter(is_active=True)
        
        # Filter out expired notices (where expiry_date is in the past)
        queryset = queryset.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gt=timezone.now())
        )
        
        # Filter by notice type if provided
        notice_type = request.query_params.get('notice_type')
        if notice_type:
            queryset = queryset.filter(notice_type=notice_type)
        
        # Order by priority and date
        queryset = queryset.order_by('-priority', '-created_at')
        
        # Limit results if provided
        limit = request.query_params.get('limit')
        if limit:
            try:
                limit = int(limit)
                queryset = queryset[:limit]
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
        # Require authentication for POST
        if not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        
        # Check if user is admin/staff
        if not request.user.is_staff:
            return Response(
                {"error": "Only admins can create notices."},
                status=status.HTTP_403_FORBIDDEN,
            )
        
        serializer = NoticeSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "PATCH", "PUT", "DELETE"])
@permission_classes([AllowAny])  # Allow public access, but we check auth manually for write operations
def notice_detail(request, notice_id):
    """
    Get, update, or delete a notice.
    
    GET: Public access (no authentication required)
    PATCH/PUT/DELETE: Requires authentication and admin privileges
    """
    try:
        notice = Notice.objects.get(notice_id=notice_id)
    except Notice.DoesNotExist:
        return Response(
            {"error": "Notice not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    if request.method == "GET":
        # Public access for GET
        serializer = NoticeSerializer(notice)
        return Response(serializer.data)
    
    # For update/delete, require authentication
    if not request.user.is_authenticated:
        return Response(
            {"error": "Authentication required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    
    # For update/delete, require admin
    if not request.user.is_staff:
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
    
    # PATCH or PUT
    serializer = NoticeSerializer(notice, data=request.data, partial=(request.method == "PATCH"))
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_inbox_folders(request):
    """
    List IMAP folders with message counts (Main Admin / staff only).
    """
    if not request.user.is_authenticated:
        return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
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
