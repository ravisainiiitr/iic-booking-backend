"""API views for User Groups (equipment visibility)."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import User, UserGroup, UserGroupMember
from iic_booking.users.models.user_type import UserType
from iic_booking.equipment.models import Equipment


def _require_admin_or_manager(user):
    """Return True if user is admin or manager (can manage user groups)."""
    return user.user_type in [UserType.ADMIN, UserType.MANAGER, UserType.OPERATOR]


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def user_group_list_create(request):
    """List all user groups or create a new one. Admin/Manager/Operator only."""
    if not _require_admin_or_manager(request.user):
        return Response(
            {"error": "Only admins, managers, and operators can manage user groups."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        groups = UserGroup.objects.all().order_by("name")
        data = [
            {
                "id": g.id,
                "name": g.name,
                "code": g.code,
                "description": g.description or "",
                "member_count": g.members.count(),
                "equipment_count": g.equipment.count(),
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "updated_at": g.updated_at.isoformat() if g.updated_at else None,
            }
            for g in groups
        ]
        return Response({"user_groups": data, "count": len(data)}, status=status.HTTP_200_OK)

    # POST
    name = request.data.get("name")
    code = request.data.get("code")
    description = request.data.get("description", "")
    if not name or not code:
        return Response(
            {"error": "name and code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    code = code.strip().upper()
    if UserGroup.objects.filter(code=code).exists():
        return Response(
            {"error": f"A group with code '{code}' already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    group = UserGroup.objects.create(name=name.strip(), code=code, description=description or None)
    return Response(
        {
            "id": group.id,
            "name": group.name,
            "code": group.code,
            "description": group.description or "",
            "member_count": 0,
            "equipment_count": 0,
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "updated_at": group.updated_at.isoformat() if group.updated_at else None,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def user_group_detail(request, pk):
    """Retrieve, update, or delete a user group. Admin/Manager/Operator only."""
    if not _require_admin_or_manager(request.user):
        return Response(
            {"error": "Only admins, managers, and operators can manage user groups."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        group = UserGroup.objects.get(pk=pk)
    except UserGroup.DoesNotExist:
        return Response({"error": "User group not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        members = [
            {
                "id": m.id,
                "user_id": m.user.id,
                "email": m.user.email,
                "name": m.user.name or "",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in group.members.select_related("user").all()
        ]
        equipment_ids = list(group.equipment.values_list("equipment_id", flat=True))
        return Response(
            {
                "id": group.id,
                "name": group.name,
                "code": group.code,
                "description": group.description or "",
                "members": members,
                "member_count": len(members),
                "equipment_ids": equipment_ids,
                "equipment_count": len(equipment_ids),
                "created_at": group.created_at.isoformat() if group.created_at else None,
                "updated_at": group.updated_at.isoformat() if group.updated_at else None,
            },
            status=status.HTTP_200_OK,
        )

    if request.method == "PUT":
        name = request.data.get("name")
        code = request.data.get("code")
        description = request.data.get("description")
        if name is not None:
            group.name = name.strip()
        if code is not None:
            code = code.strip().upper()
            if code != group.code and UserGroup.objects.filter(code=code).exists():
                return Response(
                    {"error": f"A group with code '{code}' already exists."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            group.code = code
        if description is not None:
            group.description = description.strip() or None
        group.save()
        return Response(
            {
                "id": group.id,
                "name": group.name,
                "code": group.code,
                "description": group.description or "",
                "member_count": group.members.count(),
                "equipment_count": group.equipment.count(),
                "created_at": group.created_at.isoformat() if group.created_at else None,
                "updated_at": group.updated_at.isoformat() if group.updated_at else None,
            },
            status=status.HTTP_200_OK,
        )

    # DELETE
    group.delete()
    return Response({"message": "User group deleted."}, status=status.HTTP_200_OK)


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def user_group_members(request, pk):
    """List members, add a member, or remove a member. Admin/Manager/Operator only."""
    if not _require_admin_or_manager(request.user):
        return Response(
            {"error": "Only admins, managers, and operators can manage user groups."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        group = UserGroup.objects.get(pk=pk)
    except UserGroup.DoesNotExist:
        return Response({"error": "User group not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        members = [
            {
                "id": m.id,
                "user_id": m.user.id,
                "email": m.user.email,
                "name": m.user.name or "",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in group.members.select_related("user").all()
        ]
        return Response({"members": members, "count": len(members)}, status=status.HTTP_200_OK)

    if request.method == "POST":
        user_id = request.data.get("user_id")
        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = User.objects.get(pk=int(user_id))
        except (User.DoesNotExist, ValueError, TypeError):
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        if UserGroupMember.objects.filter(user_group=group, user=user).exists():
            return Response(
                {"error": "User is already a member of this group."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership = UserGroupMember.objects.create(user_group=group, user=user)
        return Response(
            {
                "id": membership.id,
                "user_id": user.id,
                "email": user.email,
                "name": user.name or "",
                "created_at": membership.created_at.isoformat() if membership.created_at else None,
            },
            status=status.HTTP_201_CREATED,
        )

    # DELETE - remove a member (user_id in body or query)
    user_id = request.data.get("user_id") or request.query_params.get("user_id")
    if not user_id:
        return Response(
            {"error": "user_id is required (body or query)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user = User.objects.get(pk=int(user_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    deleted, _ = UserGroupMember.objects.filter(user_group=group, user=user).delete()
    if not deleted:
        return Response(
            {"error": "User is not a member of this group."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({"message": "Member removed."}, status=status.HTTP_200_OK)


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def user_group_equipment(request, pk):
    """List equipment assigned to this group, assign equipment, or unassign. Admin/Manager/Operator only."""
    if not _require_admin_or_manager(request.user):
        return Response(
            {"error": "Only admins, managers, and operators can manage user groups."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        group = UserGroup.objects.get(pk=pk)
    except UserGroup.DoesNotExist:
        return Response({"error": "User group not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        equipment_list = [
            {
                "equipment_id": e.equipment_id,
                "code": e.code,
                "name": e.name,
                "status": e.status,
            }
            for e in group.equipment.all()
        ]
        return Response(
            {"equipment": equipment_list, "count": len(equipment_list)},
            status=status.HTTP_200_OK,
        )

    if request.method == "POST":
        equipment_ids = request.data.get("equipment_ids")
        if not equipment_ids or not isinstance(equipment_ids, list):
            return Response(
                {"error": "equipment_ids (array) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = 0
        for eid in equipment_ids:
            try:
                equip = Equipment.objects.get(pk=int(eid))
                if equip.visibility_group_id != group.id:
                    equip.visibility_group = group
                    equip.save()
                    updated += 1
            except (Equipment.DoesNotExist, ValueError, TypeError):
                continue
        return Response(
            {"message": f"Assigned {updated} equipment to group.", "assigned_count": updated},
            status=status.HTTP_200_OK,
        )

    # DELETE - unassign equipment (equipment_ids in body)
    equipment_ids = request.data.get("equipment_ids")
    if not equipment_ids or not isinstance(equipment_ids, list):
        return Response(
            {"error": "equipment_ids (array) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    updated = Equipment.objects.filter(
        equipment_id__in=[int(x) for x in equipment_ids if str(x).isdigit()],
        visibility_group=group,
    ).update(visibility_group=None)
    return Response(
        {"message": f"Unassigned {updated} equipment from group.", "unassigned_count": updated},
        status=status.HTTP_200_OK,
    )
