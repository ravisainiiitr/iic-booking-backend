"""Moderation API: reports, post actions, lock, move, queue, dashboard, audit log and Community Admins."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..models import ConnectRole
from ..models import ConnectRoleAssignment
from ..models import ModerationAction
from ..models import ReportReason
from ..permissions import PortalAdmin
from ..services import moderation as msvc
from ..services.threads import NotAllowed
from ..services.threads import ThreadError
from .base import ConnectAPIView
from .communities import ConnectPagination
from .presenters import annotate_cards
from .presenters import thread_detail
from .presenters import user_label
from .threads import _error
from .threads import _int
from .threads import _post_or_404

TAG = ["IITR Connect: moderation"]


def _excerpt(text: str, n: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _post_row(p, extra=None) -> dict:
    row = {
        "post_id": p.pk,
        "thread_id": p.thread_id,
        "thread_title": p.thread.title,
        "community": {"id": p.thread.community_id, "name": p.thread.community.name},
        "author": {"id": p.author_id, "name": user_label(p.author)},
        "excerpt": _excerpt(p.body_text),
        "visibility": p.visibility,
        "is_opening": p.is_opening,
        "created_at": p.created_at,
    }
    row.update(extra or {})
    return row


class ReportView(ConnectAPIView):
    throttle_scope = "connect_report"

    @extend_schema(tags=TAG, summary="Report someone else's post (once per post)")
    def post(self, request, pk):
        post = _post_or_404(request.user, pk)
        try:
            msvc.report(request.user, post, str(request.data.get("reason") or ""), str(request.data.get("details") or ""))
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response({"reported": True}, status=201)

    @extend_schema(tags=TAG, summary="Report reasons")
    def get(self, request, pk):
        _post_or_404(request.user, pk)
        return Response({"reasons": [{"key": r.value, "label": r.label} for r in ReportReason]})


class ModeratePostView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Hide, remove or restore a post (moderators; reason required to hide or remove)")
    def post(self, request, pk):
        post = _post_or_404(request.user, pk)
        try:
            if request.data.get("action") == "dismiss_reports":
                msvc.dismiss_reports(request.user, post, str(request.data.get("reason") or ""))
            else:
                post = msvc.moderate_post(request.user, post, str(request.data.get("action") or ""), str(request.data.get("reason") or ""))
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response({"post_id": post.pk, "visibility": post.visibility})


class ThreadLockView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Lock or unlock a thread (moderators)")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        try:
            msvc.set_lock(request.user, thread, request.data.get("locked") is True, str(request.data.get("reason") or ""))
        except NotAllowed as exc:
            return _error(exc)
        return Response(thread_detail(annotate_cards(access.visible_threads(request.user).filter(pk=pk), request.user).get(), request.user))


class ThreadMoveView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Change the category (moderators) or move to another community (Community Admins, confirm_widen)")
    def post(self, request, pk):
        thread = access.get_thread_or_404(request.user, pk)
        d = request.data
        try:
            msvc.move(
                request.user,
                thread,
                category_id=(d.get("category_id") if "category_id" in d else None),
                community_id=_int(d.get("community_id")),
                confirm_widen=d.get("confirm_widen") is True,
                reason=str(d.get("reason") or ""),
            )
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return Response(thread_detail(annotate_cards(access.visible_threads(request.user).filter(pk=pk), request.user).get(), request.user))


class ModerationQueueView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Reported and hidden posts in the communities you moderate")
    def get(self, request):
        if not access.snapshot(request.user).moderated:
            raise Http404
        reported, hidden = msvc.queue(request.user, _int(request.query_params.get("community")))
        from ..models import DiscussionReport
        from ..models import ReportStatus

        reported = list(reported[:100])
        reasons: dict[int, list] = {}
        for r in DiscussionReport.objects.filter(post_id__in=[p.pk for p in reported], status=ReportStatus.OPEN).select_related("reporter"):
            reasons.setdefault(r.post_id, []).append({"reason": r.reason, "details": r.details, "reporter": user_label(r.reporter), "created_at": r.created_at})
        return Response(
            {
                "reported": [_post_row(p, {"open_reports": p.open_reports, "reports": reasons.get(p.pk, [])}) for p in reported],
                "hidden": [_post_row(p) for p in hidden[:100]],
            }
        )


class ModerationDashboardView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Moderation totals per community you moderate")
    def get(self, request):
        if not access.snapshot(request.user).moderated:
            raise Http404
        return Response(msvc.dashboard(request.user))


class ModerationLogView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Moderation audit log (communities you moderate; Portal / Community Admins see all)")
    def get(self, request):
        snap = access.snapshot(request.user)
        if not snap.moderated:
            raise Http404
        qs = ModerationAction.objects.select_related("actor", "community").order_by("-created_at")
        if not snap.is_admin:
            qs = qs.filter(community_id__in=snap.moderated)
        community = _int(request.query_params.get("community"))
        if community:
            qs = qs.filter(community_id=community)
        for key in ("target_type", "target_id", "action"):
            if request.query_params.get(key):
                qs = qs.filter(**{key: request.query_params.get(key)})
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            [
                {
                    "id": a.pk,
                    "action": a.action,
                    "actor": user_label(a.actor),
                    "community": a.community.name if a.community_id else "IITR Connect",
                    "target_type": a.target_type,
                    "target_id": a.target_id,
                    "reason": a.reason,
                    "before": a.before,
                    "after": a.after,
                    "created_at": a.created_at,
                }
                for a in page
            ]
        )


class CommunityAdminsView(ConnectAPIView):
    permission_classes = [*ConnectAPIView.permission_classes, PortalAdmin]
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Community Admins (Portal Admin only)")
    def get(self, request):
        rows = ConnectRoleAssignment.objects.filter(role=ConnectRole.COMMUNITY_ADMIN, revoked_at__isnull=True).select_related("user")
        return Response({"results": [{"user_id": r.user_id, "name": user_label(r.user), "email": r.user.email, "granted_at": r.granted_at} for r in rows]})

    @extend_schema(tags=TAG, summary="Appoint a Community Admin by e-mail (Portal Admin only)")
    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        user = get_user_model().objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user is None:
            return Response({"detail": "No active portal user has that e-mail.", "field": "email"}, status=400)
        try:
            msvc.grant_community_admin(request.user, user)
        except (ThreadError, NotAllowed) as exc:
            return _error(exc)
        return self.get(request)

    @extend_schema(tags=TAG, summary="Remove a Community Admin (Portal Admin only)")
    def delete(self, request):
        user = get_user_model().objects.filter(pk=_int(request.data.get("user_id"))).first()
        if user is None:
            raise Http404
        msvc.revoke_community_admin(request.user, user)
        return self.get(request)
