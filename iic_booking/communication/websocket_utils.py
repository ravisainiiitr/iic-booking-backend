"""Utility functions for sending WebSocket notifications."""

import json
import logging
from typing import Dict, Any, Optional

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


def send_notification_to_user(
    user_id: int,
    notification_data: Dict[str, Any],
) -> None:
    """
    Send a real-time notification to a specific user via WebSocket.
    
    Args:
        user_id: ID of the user to send notification to
        notification_data: Dictionary containing notification data
            Expected keys:
            - type: Notification type (e.g., "booking", "system", "warning", "info")
            - title: Notification title
            - message: Notification message
            - link: Optional link URL
            - metadata: Optional additional metadata
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("Channel layer not configured. WebSocket notification not sent.")
            return
        
        group_name = f"notifications_{user_id}"
        
        # Prepare notification message
        message = {
            "type": "notification_message",
            "message": {
                "type": "notification",
                "data": {
                    "id": notification_data.get("id"),
                    "title": notification_data.get("title", ""),
                    "message": notification_data.get("message", ""),
                    "type": notification_data.get("type", "info"),
                    "read": notification_data.get("read", False),
                    "created_at": notification_data.get("created_at"),
                    "link": notification_data.get("link"),
                    "metadata": notification_data.get("metadata", {}),
                },
            },
        }
        
        # Send to user's notification group
        async_to_sync(channel_layer.group_send)(group_name, message)
        logger.info(f"WebSocket notification sent to user {user_id}: {notification_data.get('title', 'No title')}")
        
    except Exception as e:
        logger.error(f"Failed to send WebSocket notification to user {user_id}: {str(e)}", exc_info=True)


def send_notification_from_communication_log(
    communication_log,
    notification_type: str = "info",
    link: Optional[str] = None,
) -> None:
    """
    Send a WebSocket notification from a CommunicationLog instance.
    
    Args:
        communication_log: CommunicationLog instance
        notification_type: Type of notification (booking, system, warning, info)
        link: Optional link URL for the notification
    """
    if not communication_log or not communication_log.recipient:
        return
    
    user_id = communication_log.recipient.id
    
    # Determine notification type from communication type
    if communication_log.communication_type == "push_notification":
        # Use the notification type from metadata if available
        metadata = communication_log.metadata or {}
        notification_type = metadata.get("notification_type", notification_type)
    
    notification_data = {
        "id": communication_log.id,
        "title": communication_log.subject or "Notification",
        "message": communication_log.message or "",
        "type": notification_type,
        "read": False,
        "created_at": communication_log.created_at.isoformat() if communication_log.created_at else None,
        "link": link,
        "metadata": {
            "communication_log_id": communication_log.id,
            "communication_type": communication_log.communication_type,
            **(communication_log.metadata or {}),
        },
    }
    
    send_notification_to_user(user_id, notification_data)
