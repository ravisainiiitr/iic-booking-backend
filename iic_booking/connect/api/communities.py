"""Communities, members and membership administration.

Visibility
* Lists and counts cover only communities the caller can read (``access``). Nothing about other communities is
  returned, including their existence.
* Member lists: moderators and admins see every member with status and sources; other members see only the
  community's staff (OIC, Lab Operators, moderators). E-mail addresses are shown to admins only.

Authority for member actions
* Moderators: suspend posting / restore a suspension, for ordinary members only (not staff, moderators or admins).
* Community Admin / Portal Admin: every action (suspend, revoke, restore, add, appoint / remove / reset moderator).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.db.models import Q
from django.http import Http404
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .. import access
from ..models import Community
from ..models import CommunityKind
from ..models import CommunityMembership
from ..models import CommunitySyncRun
from ..models import DiscussionCategory
from ..models import MembershipAuditLog
from ..models import MembershipStatus
from ..permissions import ConnectAdmin
from ..permissions import PortalAdmin
from ..services import membership as membership_service
from .base import ConnectAPIView

TAG = ["IITR Connect: communities"]


class ConnectPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def user_label(user) -> str:
    return (getattr(user, "name", "") or "").strip() or "Portal user"


def _staff_ids(community_ids) -> dict[int, set[int]]:
    rows = CommunityMembership.objects.filter(community_id__in=list(community_ids), status=MembershipStatus.ACTIVE).filter(
        Q(via_oic=True) | Q(via_operator=True) | Q(moderator_override=True)
    )
    out: dict[int, set[int]] = {}
    for cid, uid in rows.values_list("community_id", "user_id"):
        out.setdefault(cid, set()).add(uid)
    return out


def community_payload(community: Community, user, *, member_count=None, thread_count=None, categories=None) -> dict:
    snap = access.snapshot(user)
    membership = snap.memberships.get(community.pk)
    data = {
        "id": community.pk,
        "slug": community.slug,
        "name": community.name,
        "kind": community.kind,
        "status": community.status,
        "description": community.description,
        "equipment_group_id": community.equipment_group_ref_id,
        "is_test_only": community.is_test_only,
        "member_count": member_count,
        "thread_count": thread_count,
        "can_post": access.can_post_in(user, community.pk),
        "can_moderate": access.can_moderate(user, community.pk),
        "my_badge": access.role_badge(user, community.pk),
        "my_status": membership.status if membership else ("active" if community.is_general else None),
    }
    if categories is not None:
        data["categories"] = categories
    return data


def _visible_equipment_for(user, group_id: int) -> list[dict]:
    from iic_booking.equipment.api_views import get_visible_equipment_queryset

    qs = get_visible_equipment_queryset(user, catalog_scope="all", mode_catalog_filter=False).filter(
        Q(equipment_group_id=group_id) | Q(parent_equipment__equipment_group_id=group_id, equipment_group_id__isnull=True)
    )
    return list(qs.order_by("name").values("equipment_id", "name", "code")[:200])


class CommunityListView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Communities the caller can read (My Communities + General)")
    def get(self, request):
        user = request.user
        communities = list(access.visible_communities(user).order_by("kind", "name"))
        ids = [c.pk for c in communities]
        members = membership_service.member_counts(ids)
        threads = dict(
            access.visible_threads(user).values("community_id").annotate(n=Count("pk")).values_list("community_id", "n")
        )
        out = [
            community_payload(c, user, member_count=None if c.is_general else members.get(c.pk, 0), thread_count=threads.get(c.pk, 0))
            for c in communities
        ]
        return Response({"results": out})


class CommunityDetailView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="One community the caller can read (404 otherwise)")
    def get(self, request, slug):
        user = request.user
        community = access.get_community_or_404(user, slug=slug)
        categories = list(
            DiscussionCategory.objects.filter(community=community, is_active=True)
            .order_by("position", "name")
            .values("id", "slug", "name", "description", "staff_only_posting")
        )
        data = community_payload(
            community,
            user,
            member_count=None if community.is_general else membership_service.member_counts([community.pk]).get(community.pk, 0),
            thread_count=access.visible_threads(user).filter(community=community).count(),
            categories=categories,
        )
        if community.kind == CommunityKind.GROUP and community.equipment_group_ref_id:
            data["equipment"] = _visible_equipment_for(user, community.equipment_group_ref_id)
        staff = CommunityMembership.objects.filter(community=community, status=MembershipStatus.ACTIVE).filter(
            Q(via_oic=True) | Q(via_operator=True) | Q(moderator_override=True)
        ).select_related("user")
        data["staff"] = [
            {"user_id": m.user_id, "name": user_label(m.user), "badge": _badge_of(m)}
            for m in staff
            if m.user.is_active
        ]
        from ..models import DiscussionSubscription

        data["following"] = DiscussionSubscription.objects.filter(user=user, community=community, level="watching").exists()
        return Response(data)


def _badge_of(m: CommunityMembership) -> str:
    if m.via_oic:
        return access.BADGE_OIC
    if m.via_operator:
        return access.BADGE_OPERATOR
    if m.moderator_override is True:
        return access.BADGE_MODERATOR
    return ""


class CommunityMembersView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Members (full list for moderators and admins; staff only for members)")
    def get(self, request, slug):
        user = request.user
        community = access.get_community_or_404(user, slug=slug)
        snap = access.snapshot(user)
        full = access.can_moderate(user, community.pk)
        qs = CommunityMembership.objects.filter(community=community).select_related("user").order_by("user__name", "pk")
        if not full:
            qs = qs.filter(status=MembershipStatus.ACTIVE).filter(
                Q(via_oic=True) | Q(via_operator=True) | Q(moderator_override=True)
            )
        else:
            status_filter = request.query_params.get("status")
            if status_filter in MembershipStatus.values:
                qs = qs.filter(status=status_filter)
            q = (request.query_params.get("q") or "").strip()[:80]
            if q:
                qs = qs.filter(Q(user__name__icontains=q) | Q(user__email__icontains=q))
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        rows = []
        for m in page:
            row = {"user_id": m.user_id, "name": user_label(m.user), "badge": _badge_of(m), "role": m.role}
            if full:
                row.update(
                    {
                        "status": m.status,
                        "via_booking": m.via_booking,
                        "via_oic": m.via_oic,
                        "via_operator": m.via_operator,
                        "via_manual": m.via_manual,
                        "moderator_override": m.moderator_override,
                        "booking_count": m.booking_count,
                        "last_booked_at": m.last_booked_at,
                        "joined_at": m.joined_at,
                        "status_reason": m.status_reason,
                        "suspended_until": m.suspended_until,
                    }
                )
            if snap.is_admin:
                row["email"] = m.user.email
            rows.append(row)
        return paginator.get_paginated_response(rows)


MODERATOR_ACTIONS = {"suspend", "restore"}


class CommunityMemberActionView(ConnectAPIView):
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Suspend, revoke, restore, add or change the moderator role of a member (audited)")
    def post(self, request, slug, user_id):
        actor = request.user
        community = access.get_community_or_404(actor, slug=slug)
        snap = access.snapshot(actor)
        action = str(request.data.get("action") or "")
        if action not in membership_service.MANUAL_ACTIONS:
            return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)
        if not access.can_moderate(actor, community.pk):
            raise Http404
        target = get_user_model().objects.filter(pk=user_id).first()
        if target is None:
            raise Http404
        existing = CommunityMembership.objects.filter(community=community, user=target).first()
        if not snap.is_admin:
            if action not in MODERATOR_ACTIONS:
                return Response({"detail": "Only a Community Admin can do this."}, status=status.HTTP_403_FORBIDDEN)
            if existing is None and not community.is_general:
                raise Http404
            target_is_staff = existing is not None and (existing.is_staff_source or existing.moderator_override is True)
            if target_is_staff or access.is_portal_admin(target) or target.pk == actor.pk:
                return Response(
                    {"detail": "Moderators cannot change the access of staff, moderators or themselves."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if community.is_general and not access.is_internal_user(target):
                raise Http404
        until = parse_datetime(str(request.data.get("until") or "")) if request.data.get("until") else None
        try:
            m = membership_service.manual_action(
                community, target, action, actor=actor, reason=str(request.data.get("reason") or ""), until=until
            )
        except membership_service.MembershipActionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"user_id": target.pk, "status": m.status, "role": m.role, "moderator_override": m.moderator_override})


class CommunityAuditView(ConnectAPIView):
    permission_classes = [*ConnectAPIView.permission_classes, ConnectAdmin]

    @extend_schema(tags=TAG, summary="Membership audit log of one community (admins)")
    def get(self, request, slug):
        community = access.get_community_or_404(request.user, slug=slug)
        qs = MembershipAuditLog.objects.filter(community=community).select_related("user", "actor").order_by("-created_at")
        uid = request.query_params.get("user")
        if uid and str(uid).isdigit():
            qs = qs.filter(user_id=int(uid))
        paginator = ConnectPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            [
                {
                    "id": row.pk,
                    "user_id": row.user_id,
                    "user": user_label(row.user),
                    "actor": user_label(row.actor) if row.actor_id else "Automatic sync",
                    "action": row.action,
                    "detail": row.detail,
                    "created_at": row.created_at,
                }
                for row in page
            ]
        )


class AdminSyncView(ConnectAPIView):
    """Portal Admin only. Dry run by default; ``apply: true`` writes. Runs inline (an admin action, not a hot path)."""

    permission_classes = [*ConnectAPIView.permission_classes, PortalAdmin]
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="Recent membership sync runs")
    def get(self, request):
        runs = CommunitySyncRun.objects.order_by("-started_at")[:20]
        return Response(
            {
                "results": [
                    {
                        "id": r.pk,
                        "scope": r.scope,
                        "dry_run": r.dry_run,
                        "status": r.status,
                        "source": r.source,
                        "started_at": r.started_at,
                        "finished_at": r.finished_at,
                        "summary": {k: v for k, v in (r.report or {}).items() if k not in ("per_community",)},
                    }
                    for r in runs
                ]
            }
        )

    @extend_schema(tags=TAG, summary="Run the membership sync (dry run unless apply=true)")
    def post(self, request):
        apply = request.data.get("apply") is True
        raw_groups = request.data.get("group_ids")
        group_ids = None
        if isinstance(raw_groups, list) and raw_groups:
            try:
                group_ids = [int(g) for g in raw_groups][:200]
            except (TypeError, ValueError):
                return Response({"detail": "group_ids must be integers."}, status=status.HTTP_400_BAD_REQUEST)
        report = membership_service.sync_all(dry_run=not apply, group_ids=group_ids, triggered_by=request.user, source="admin")
        return Response(report.as_dict())


__all__ = [
    "AdminSyncView",
    "CommunityAuditView",
    "CommunityDetailView",
    "CommunityListView",
    "CommunityMemberActionView",
    "CommunityMembersView",
    "ConnectPagination",
]
