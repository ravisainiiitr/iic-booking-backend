"""WebSocket ``ws/calls/?token=``: incoming-call and state events for the signed-in user, presence (who can be rung
right now), and WebRTC signalling relayed between the two participants of an accepted call.

The server never decides what audio flows; it only forwards offer / answer / ICE candidate messages, and only:
- from a participant of the call (the socket's own user),
- while the call is accepted (no media negotiation before the callee answers),
- with a whitelisted shape and size (no injection of arbitrary messages into the other side).
"""

from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache

from .recipients import PRESENCE_KEY, PRESENCE_TTL
from .service import group_for

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 16 * 1024
SIGNAL_KINDS = {"offer", "answer", "candidate", "bye"}


class CallConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        from urllib.parse import parse_qs

        token = (parse_qs(self.scope.get("query_string", b"").decode()).get("token") or [""])[0]
        user = await self._authenticate(token)
        if user is None:
            await self.close(code=4001)
            return
        self.user_id = user.pk
        self.group_name = group_for(user.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._touch_presence()
        await self.send(text_data=json.dumps({"type": "ready"}))

    async def disconnect(self, code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data or len(text_data) > MAX_MESSAGE_BYTES or not hasattr(self, "user_id"):
            return
        try:
            data = json.loads(text_data)
        except ValueError:
            return
        kind = data.get("type")
        if kind == "ping":
            await self._touch_presence()
            await self.send(text_data=json.dumps({"type": "pong"}))
        elif kind == "signal":
            await self._relay(data)

    async def call_message(self, event):
        await self.send(text_data=json.dumps(event["message"]))

    async def _relay(self, data):
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("kind") not in SIGNAL_KINDS:
            return
        clean = {"kind": payload["kind"]}
        if payload["kind"] in ("offer", "answer"):
            if not isinstance(payload.get("sdp"), str):
                return
            clean["sdp"] = payload["sdp"]
        elif payload["kind"] == "candidate":
            cand = payload.get("candidate")
            if not isinstance(cand, dict) or not isinstance(cand.get("candidate", ""), str):
                return
            clean["candidate"] = {k: cand.get(k) for k in ("candidate", "sdpMid", "sdpMLineIndex", "usernameFragment")}
        other = await self._other_participant(str(data.get("call_id") or ""))
        if other is None:
            return
        await self.channel_layer.group_send(group_for(other), {"type": "call.message", "message": {
            "type": "signal", "call_id": str(data["call_id"]), "payload": clean}})

    @database_sync_to_async
    def _other_participant(self, call_id):
        from .models import CallSession, CallStatus

        try:
            session = CallSession.objects.only("caller_id", "callee_id", "status").get(pk=call_id)
        except (CallSession.DoesNotExist, ValueError, Exception):  # noqa: BLE001 - malformed id
            return None
        if session.status != CallStatus.ACCEPTED or self.user_id not in (session.caller_id, session.callee_id):
            return None
        return session.callee_id if self.user_id == session.caller_id else session.caller_id

    @database_sync_to_async
    def _touch_presence(self):
        cache.set(PRESENCE_KEY.format(self.user_id), 1, PRESENCE_TTL)

    @database_sync_to_async
    def _authenticate(self, token):
        from rest_framework.authtoken.models import Token

        from iic_booking.users.mobile_sessions import ACCESS_PREFIX, authenticate_mobile_access_key

        from .recipients import calls_open_to

        if not token:
            return None
        user = None
        if token.startswith(ACCESS_PREFIX):  # the app's mobile access key
            result = authenticate_mobile_access_key(token)
            user = result[0] if result else None
        else:
            found = Token.objects.select_related("user").filter(key=token).first()
            user = found.user if found and found.user.is_active else None

        return user if user is not None and calls_open_to(user) else None
