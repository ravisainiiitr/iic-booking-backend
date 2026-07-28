"""Activity feed service."""

from __future__ import annotations

from iic_booking.remote_analysis.collaboration_models import ActivityEvent, ActivityFeed, CollaborationTelemetry
from iic_booking.remote_analysis.constants import ActivityVerb


class ActivityService:
    def get_feed(self, user=None, *, name: str = "default") -> ActivityFeed:
        feed, _ = ActivityFeed.objects.get_or_create(user=user, name=name)
        return feed

    def record(
        self,
        verb: str,
        summary: str,
        *,
        actor=None,
        user=None,
        details: str = "",
        session=None,
        workspace=None,
        reservation=None,
        metadata: dict | None = None,
        also_global: bool = True,
    ) -> ActivityEvent:
        feed = self.get_feed(user)
        event = ActivityEvent.objects.create(
            feed=feed,
            actor=actor if actor is not None and getattr(actor, "pk", None) else None,
            verb=verb,
            summary=summary[:512],
            details=details,
            session=session,
            workspace=workspace,
            reservation=reservation,
            metadata=metadata or {},
        )
        if also_global and user is not None:
            gfeed = self.get_feed(None, name="platform")
            ActivityEvent.objects.create(
                feed=gfeed,
                actor=event.actor,
                verb=verb,
                summary=summary[:512],
                details=details,
                session=session,
                workspace=workspace,
                reservation=reservation,
                metadata=metadata or {},
            )
        CollaborationTelemetry.objects.create(
            metric_name="activity_volume",
            value=1.0,
            unit="event",
            tags={"verb": verb},
        )
        return event

    def list_events(self, user=None, *, limit: int = 100, verb: str | None = None) -> list[ActivityEvent]:
        feed = self.get_feed(user)
        qs = ActivityEvent.objects.filter(feed=feed).select_related("actor", "session", "workspace")
        if verb:
            qs = qs.filter(verb=verb)
        return list(qs.order_by("-created_at")[:limit])
