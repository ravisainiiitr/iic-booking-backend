"""Main Administrator settings for support desk notifications."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import UserType

from .email_lists import MAX_ALERT_RECIPIENTS, clean_email_list, split_emails
from .models import SupportNotificationSettings


def _is_main_admin(user) -> bool:
    return bool(user and getattr(user, "user_type", None) == UserType.ADMIN)


def _payload(cfg: SupportNotificationSettings) -> dict:
    from iic_booking.users.test_accounts import parse_email_list

    updated_by = cfg.updated_by
    return {
        "ticket_alert_enabled": bool(cfg.ticket_alert_enabled),
        "ticket_alert_emails": parse_email_list(cfg.ticket_alert_emails),
        "default_ticket_alert_emails": [],
        "max_recipients": MAX_ALERT_RECIPIENTS,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        "updated_by_name": (updated_by.get_display_name() if updated_by else None),
    }


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def support_notification_settings(request):
    """Main Administrator: who gets a copy of every new support ticket."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator access required"}, status=status.HTTP_403_FORBIDDEN)

    cfg = SupportNotificationSettings.get_singleton()
    if request.method == "GET":
        return Response(_payload(cfg))

    data = request.data or {}
    enabled = cfg.ticket_alert_enabled
    if "ticket_alert_enabled" in data:
        raw_enabled = data.get("ticket_alert_enabled")
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on")
        else:
            enabled = bool(raw_enabled)

    if "ticket_alert_emails" in data:
        emails, invalid = clean_email_list(data.get("ticket_alert_emails"))
        if invalid:
            return Response(
                {"error": f"Invalid email address: {', '.join(invalid[:5])}", "invalid": invalid},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(emails) > MAX_ALERT_RECIPIENTS:
            return Response(
                {"error": f"At most {MAX_ALERT_RECIPIENTS} recipients are allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        emails = split_emails(cfg.ticket_alert_emails)

    if enabled and not emails:
        return Response(
            {"error": "Add at least one recipient, or switch new ticket emails off."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    cfg.ticket_alert_enabled = enabled
    cfg.ticket_alert_emails = ", ".join(emails)
    cfg.updated_by = request.user
    cfg.save()
    return Response(_payload(cfg))
