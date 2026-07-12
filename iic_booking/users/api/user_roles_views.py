"""API views for user roles (e.g. check admin panel access)."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.contrib.auth import get_user_model
from iic_booking.users.models.user_type import UserType

User = get_user_model()

# Map backend user_type to frontend role key (UserManagement roleLabels).
USER_TYPE_TO_ROLE = {
    UserType.ADMIN: "admin",
    UserType.MANAGER: "officer_in_charge",
    UserType.OPERATOR: "operator",
    UserType.FINANCE: "accounts",
    UserType.STUDENT: "iitr_student",
    UserType.INDIVIDUAL_STUDENT: "iitr_student",
    UserType.FACULTY: "iitr_faculty",
    UserType.EXTERNAL: "external_academic",
    UserType.RND: "external_rnd",
    UserType.INSTITUTE: "external_academic",
    UserType.OTHER: "industrial_user",
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_roles_list(request):
    """
    List roles for a user. Used by User Management to show user roles.

    Query params:
        user_id: User ID. Only admin-panel users can query other users;
            others can only query themselves.

    Returns:
        200: List of role objects, e.g. [{"role": "admin"}].
        This backend has a single user_type per user, so the list has one item.
    """
    user_id_param = request.query_params.get("user_id")
    if not user_id_param:
        return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        user_id = int(user_id_param)
    except (TypeError, ValueError):
        return Response({"error": "Invalid user_id"}, status=status.HTTP_400_BAD_REQUEST)

    if user_id != request.user.id and request.user.user_type not in UserType.get_admin_panel_codes():
        return Response(
            {"error": "You can only view your own roles unless you are an admin-panel user."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    role_key = USER_TYPE_TO_ROLE.get(user.user_type, user.user_type or "iitr_student")
    return Response([{"role": role_key}])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def check_admin(request):
    """
    Check if the user has admin panel access (admin, manager, operator, finance).

    Query params:
        user_id (optional): User ID to check. If provided, must match the
            authenticated user (caller can only check themselves).

    Returns:
        200: { "is_admin": true } or { "is_admin": false }
    """
    user_id_param = request.query_params.get("user_id")
    if user_id_param is not None:
        try:
            user_id = int(user_id_param)
        except (TypeError, ValueError):
            return Response({"error": "Invalid user_id"}, status=400)
        if user_id != request.user.id:
            return Response(
                {"error": "You can only check your own admin status"},
                status=403,
            )
    user = request.user
    is_admin = user.user_type in UserType.get_admin_panel_codes()
    return Response({"is_admin": is_admin})
