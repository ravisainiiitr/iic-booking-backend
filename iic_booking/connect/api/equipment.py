"""Equipment page Discussions panel and staff tools on threads."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..services import staff_tools
from ..services.threads import NotAllowed
from .base import ConnectAPIView
from .knowledge import article_card
from .presenters import annotate_cards
from .presenters import thread_card
from .presenters import thread_detail
from .threads import _error

TAG = ["IITR Connect: equipment"]
SECTION_LIMIT = 5


class EquipmentDiscussionsView(ConnectAPIView):
    """Discussions about one instrument for the signed-in equipment page (never the public QR page).

    404 when the caller cannot see the equipment in the catalogue. ``available: false`` with a reason when the
    discussions live in a community the caller cannot read ("members_only", "internal_only").
    """

    @extend_schema(tags=TAG, summary="Discussions panel for an equipment page")
    def get(self, request, equipment_id):
        user = request.user
        ctx = staff_tools.equipment_context(user, equipment_id)
        if not ctx["available"]:
            return Response({k: v for k, v in ctx.items() if k != "community"} | ({"community_name": ctx["community"]["name"]} if ctx.get("community") else {}))
        community = ctx["community"]
        sections = staff_tools.equipment_sections(user, community, equipment_id)
        out = {
            "available": True,
            "equipment": ctx["equipment"],
            "community": {"id": community.pk, "slug": community.slug, "name": community.name, "kind": community.kind},
            "category": {"slug": ctx["category"].slug, "name": ctx["category"].name} if ctx.get("category") else None,
            "can_post": access.can_post_in(user, community.pk),
            "counts": {
                "threads": sections["recent"].count(),
                "open_questions": sections["questions"].count(),
                "solved": sections["solutions"].count(),
                "known_problems": sections["known_problems"].count(),
            },
            "note": "Discussions never change this instrument's availability, maintenance status or bookings.",
        }
        for key in ("recent", "questions", "solutions", "known_problems", "announcements"):
            limit = 3 if key == "announcements" else SECTION_LIMIT
            out[key] = [thread_card(t) for t in annotate_cards(sections[key], user)[:limit]]
        out["articles"] = [
            article_card(a, user)
            for a in sections["articles"].select_related("community", "category", "equipment", "author", "reviewer").prefetch_related("tags")[:SECTION_LIMIT]
        ]
        return Response(out)


class ThreadStaffToolsView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(
        tags=TAG,
        summary="Pin, known problem, workaround, outdated, resolved (moderators of the community; audited; never changes equipment)",
    )
    def patch(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        changes = {k: request.data[k] for k in (*staff_tools.FLAG_FIELDS, "resolved") if k in request.data}
        try:
            staff_tools.set_flags(request.user, thread, changes, str(request.data.get("reason") or ""))
        except NotAllowed as exc:
            return _error(exc)
        return Response(thread_detail(annotate_cards(access.visible_threads(request.user).filter(pk=pk), request.user).get(), request.user))
