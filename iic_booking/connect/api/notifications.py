"""Connect notification inbox, notification preferences and announcement acknowledgements."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..models import ConnectNotificationPreference
from ..models import DiscussionNotification
from ..models import EmailMode
from ..models import NotificationEvent
from ..services import announcements as announcement_service
from ..services import notifications as notification_service
from ..services.threads import NotAllowed
from ..services.threads import ThreadError
from .base import ConnectAPIView
from .communities import ConnectPagination
from .presenters import annotate_cards
from .presenters import thread_detail
from .presenters import user_label
from .threads import _error

TAG = ["IITR Connect: notifications"]


class NotificationListView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Your Connect notifications (only items you can still read are shown)")
    def get(self, request):
        user = request.user
        qs = DiscussionNotification.objects.filter(recipient=user).select_related("thread", "community", "actor").order_by("-created_at")
        if request.query_params.get("unread") in ("1", "true"):
            qs = qs.filter(read_at__isnull=True)
        readable = access.visible_threads(user).values("pk")
        qs = qs.filter(thread_id__in=readable) | qs.filter(thread__isnull=True, article_id__in=access.visible_articles(user).values("pk"))
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs.order_by("-created_at"), request, view=self)
        rows = []
        for n in page:
            title, message = notification_service._text(n, n.thread, n.actor)
            rows.append(
                {
                    "id": n.pk,
                    "event": n.event,
                    "title": title,
                    "message": message,
                    "link": notification_service._link(n.thread_id, n.post_id, n.article_id),
                    "actor": user_label(n.actor) if n.actor_id else "",
                    "created_at": n.created_at,
                    "read": n.read_at is not None,
                }
            )
        response = paginator.get_paginated_response(rows)
        response.data["unread_count"] = notification_service.unread_count(user)
        return response


class NotificationReadView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Mark notifications read ({ids: [...]} or {all: true})")
    def post(self, request):
        ids = None if request.data.get("all") is True else (request.data.get("ids") or [])
        return Response({"marked": notification_service.mark_read(request.user, ids)})


def _pref_payload(pref: ConnectNotificationPreference) -> dict:
    return {
        "email_mode": pref.email_mode,
        "in_app_enabled": pref.in_app_enabled,
        "muted_events": list(pref.muted_events or []),
        "auto_follow_own_threads": pref.auto_follow_own_threads,
        "auto_follow_replied_threads": pref.auto_follow_replied_threads,
        "events": [{"key": e.value, "label": e.label} for e in NotificationEvent],
        "email_modes": [{"key": m.value, "label": m.label} for m in EmailMode],
    }


class NotificationPreferenceView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Your Connect notification preferences")
    def get(self, request):
        return Response(_pref_payload(notification_service.preference(request.user)))

    @extend_schema(tags=TAG, summary="Change how and how often Connect notifies you")
    def put(self, request):
        pref, _ = ConnectNotificationPreference.objects.get_or_create(user=request.user)
        d = request.data
        if "email_mode" in d:
            if d.get("email_mode") not in EmailMode.values:
                return Response({"detail": "Choose immediate, daily, weekly or off.", "field": "email_mode"}, status=400)
            pref.email_mode = d.get("email_mode")
        if "in_app_enabled" in d:
            pref.in_app_enabled = d.get("in_app_enabled") is True
        if "muted_events" in d:
            muted = d.get("muted_events") or []
            if not isinstance(muted, list) or any(m not in NotificationEvent.values for m in muted):
                return Response({"detail": "Unknown notification type.", "field": "muted_events"}, status=400)
            pref.muted_events = sorted(set(muted))
        for key in ("auto_follow_own_threads", "auto_follow_replied_threads"):
            if key in d:
                setattr(pref, key, d.get(key) is True)
        pref.save()
        return Response(_pref_payload(pref))


class AcknowledgeView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Acknowledge an announcement that asks for it")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            announcement_service.acknowledge(request.user, thread)
        except ThreadError as exc:
            return _error(exc)
        return Response(thread_detail(annotate_cards(access.visible_threads(request.user).filter(pk=pk), request.user).get(), request.user))

    @extend_schema(tags=TAG, summary="Who acknowledged (facility staff and moderators)")
    def get(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            return Response({"results": announcement_service.acknowledgement_list(request.user, thread)})
        except NotAllowed as exc:
            return _error(exc)
