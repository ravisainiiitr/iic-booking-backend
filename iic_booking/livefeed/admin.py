from django.contrib import admin

from .models import LiveFeedPeer, LiveFeedSession


class PeerInline(admin.TabularInline):
    model = LiveFeedPeer
    extra = 0
    fields = ("viewer", "kind", "status", "error", "created_at", "answered_at", "closed_at")
    readonly_fields = fields
    can_delete = False


@admin.register(LiveFeedSession)
class LiveFeedSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "equipment", "booking", "status", "sources", "audio", "started_at", "ended_at", "end_reason")
    list_filter = ("status",)
    raw_id_fields = ("equipment", "booking", "sync_agent")
    readonly_fields = ("started_at", "last_agent_poll_at", "ended_at")
    inlines = [PeerInline]
