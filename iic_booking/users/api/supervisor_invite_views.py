"""Student invites a supervisor by email; faculty resolve the invite link after signing in."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import supervisor_invites as svc
from iic_booking.users.models import SupervisorInvite
from iic_booking.users.models.user_type import UserType


def _error(err: svc.InviteError) -> Response:
    return Response({"error": err.message, "code": err.code, **err.extra}, status=err.status)


def _limits() -> dict:
    return {
        "max_active": svc.MAX_ACTIVE_INVITES_PER_STUDENT,
        "valid_days": svc.INVITE_VALID_DAYS,
        "resend_hours": int(svc.RESEND_COOLDOWN.total_seconds() // 3600),
        "allowed_domains": svc.allowed_email_domains(),
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def supervisor_invites(request):
    """GET: the student's invitations. POST: invite a supervisor who is not on the portal yet."""
    user = request.user
    if user.user_type not in svc.INVITER_USER_TYPES:
        return Response({"error": "Only students can invite a supervisor.", "code": "not_allowed"}, status=status.HTTP_403_FORBIDDEN)

    if request.method == "GET":
        mine = SupervisorInvite.objects.filter(student=user).select_related("department")
        svc.expire_stale(mine)
        rows = [svc.serialize_invite(i) for i in mine.order_by("-created_at")[:50]]
        return Response({"invites": rows, "limits": _limits()})

    data = request.data or {}
    try:
        invite = svc.create_invite(
            user,
            svc.InviteInput(
                email=data.get("email", ""),
                supervisor_name=data.get("supervisor_name", "") or "",
                department_id=data.get("department_id"),
                message=data.get("message", "") or "",
            ),
        )
    except svc.InviteError as err:
        return _error(err)
    return Response(
        {
            "invite": svc.serialize_invite(invite),
            "message": "Invitation sent. We will let you know when your supervisor signs in.",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_supervisor_invite(request, invite_id):
    try:
        invite = svc.resend_invite(request.user, invite_id)
    except svc.InviteError as err:
        return _error(err)
    return Response({"invite": svc.serialize_invite(invite), "message": "Invitation sent again."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_supervisor_invite(request, invite_id):
    try:
        invite = svc.cancel_invite(request.user, invite_id)
    except svc.InviteError as err:
        return _error(err)
    return Response({"invite": svc.serialize_invite(invite), "message": "Invitation cancelled."})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def resolve_supervisor_invite(request):
    """Faculty only: after normal sign-in, find the link request created from an invite email."""
    if request.user.user_type != UserType.FACULTY:
        return Response({"matched": False})
    token = (request.query_params.get("token") or "").strip()
    if not token:
        return Response({"matched": False})
    return Response(svc.resolve_token_for_faculty(request.user, token))
