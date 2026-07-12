"""WebSocket consumers for real-time notifications."""

import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

logger = logging.getLogger(__name__)
User = get_user_model()


class NotificationConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time user notifications."""

    async def connect(self):
        """Handle WebSocket connection with token authentication."""
        try:
            # Get token from query string
            query_string = self.scope.get("query_string", b"").decode()
            token = None
            
            # Parse query string to get token (handle URL encoding)
            if query_string:
                from urllib.parse import parse_qs
                params = parse_qs(query_string)
                if "token" in params and params["token"]:
                    token = params["token"][0]
            
            # If no token in query string, try to get from headers
            if not token:
                headers = dict(self.scope.get("headers", []))
                auth_header = headers.get(b"authorization", b"").decode()
                if auth_header.startswith("Token "):
                    token = auth_header.split(" ")[1]
            
            if not token:
                logger.warning("WebSocket connection rejected: No token provided")
                await self.close(code=4001)  # Custom close code for authentication failure
                return
            
            # Authenticate user
            user = await self.authenticate_user(token)
            if not user:
                logger.warning(f"WebSocket connection rejected: Invalid token (token: {token[:10]}...)")
                await self.close(code=4002)  # Custom close code for invalid token
                return
            
            # Store user in scope
            self.scope["user"] = user
            self.user_id = user.id
            
            # Create user-specific channel group
            self.group_name = f"notifications_{self.user_id}"
            
            # Join user's notification group
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            
            await self.accept()
            logger.info(f"WebSocket connected for user {user.email} (ID: {self.user_id})")
            
            # Send welcome message
            await self.send(text_data=json.dumps({
                "type": "connection",
                "message": "Connected to notification service",
                "user_id": self.user_id,
            }))
        except Exception as e:
            logger.error(f"Error during WebSocket connection: {str(e)}", exc_info=True)
            try:
                await self.close(code=4003)  # Custom close code for server error
            except Exception:
                pass  # Ignore errors during close

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        if hasattr(self, "group_name"):
            # Leave user's notification group
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )
            logger.info(f"WebSocket disconnected for user ID: {self.user_id}")

    async def receive(self, text_data):
        """Handle messages received from WebSocket client."""
        try:
            data = json.loads(text_data)
            message_type = data.get("type", "")
            
            if message_type == "ping":
                # Respond to ping with pong
                await self.send(text_data=json.dumps({
                    "type": "pong",
                    "timestamp": data.get("timestamp", ""),
                }))
            elif message_type == "mark_read":
                # Handle marking notification as read
                notification_id = data.get("notification_id")
                if notification_id:
                    # You can add logic here to mark notification as read in database
                    logger.info(f"User {self.user_id} marked notification {notification_id} as read")
        except json.JSONDecodeError:
            logger.error("Invalid JSON received from WebSocket client")
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {str(e)}")

    async def notification_message(self, event):
        """Send notification message to WebSocket client."""
        message = event.get("message", {})
        await self.send(text_data=json.dumps(message))

    @database_sync_to_async
    def authenticate_user(self, token_key):
        """Authenticate user from token."""
        try:
            token = Token.objects.select_related("user").get(key=token_key)
            user = token.user
            if user.is_active:
                return user
        except Token.DoesNotExist:
            pass
        return None
