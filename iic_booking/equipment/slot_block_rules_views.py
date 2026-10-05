"""
Repeat block API (Change slot status page): /api/admin/equipment/<equipment_id>/slot-block-rules/.

Main Administrator: any equipment. OIC: only equipment they manage (primary or temporary OIC).
Everyone else, including Lab Operators and Department Administrators, gets 403.
Errors arrive as {detail, code, field?}.
"""

from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from iic_booking.equipment import slot_block_rules as rules
from iic_booking.equipment.models import Equipment, RecurringSlotBlockRule
from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.users.models.user_type import UserType


def _equipment_for(request, equipment_id: int) -> Equipment:
    user = request.user
    user_type = getattr(user, "user_type", None)
    if user_type not in (UserType.ADMIN, UserType.MANAGER):
        raise PermissionDenied("Only the Main Administrator or the equipment's OIC can set repeat blocks.")
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if equipment is None:
        raise NotFound("Equipment not found.")
    if user_type == UserType.MANAGER and equipment.pk not in set(get_equipment_ids_managed_by_oic(user.id)):
        raise PermissionDenied("You can set repeat blocks only on equipment you manage as OIC.")
    return equipment


def _rule_for(equipment: Equipment, rule_id: int) -> RecurringSlotBlockRule:
    rule = (
        RecurringSlotBlockRule.objects.select_related("created_by", "removed_by", "equipment")
        .filter(pk=rule_id, equipment=equipment)
        .first()
    )
    if rule is None:
        raise NotFound("Repeat block not found.")
    return rule


def _input_error(exc: rules.RuleInputError) -> Response:
    body = {"detail": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    return Response(body, status=status.HTTP_400_BAD_REQUEST)


class SlotBlockRuleListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, equipment_id: int):
        equipment = _equipment_for(request, equipment_id)
        qs = RecurringSlotBlockRule.objects.filter(equipment=equipment).select_related("created_by", "removed_by")
        include_removed = str(request.query_params.get("include_removed", "")).lower() in ("1", "true", "yes")
        active = [rules.serialize_rule(r) for r in qs.filter(is_active=True).order_by("start_date", "created_at")]
        body = {
            "equipment": {"id": equipment.pk, "code": equipment.code, "name": equipment.name},
            "slot_times": rules.equipment_slot_times(equipment),
            "rules": active,
        }
        if include_removed:
            body["removed_rules"] = [
                rules.serialize_rule(r, with_removal_preview=False)
                for r in qs.filter(is_active=False).order_by("-removed_at")[:20]
            ]
        return Response(body)

    def post(self, request, equipment_id: int):
        equipment = _equipment_for(request, equipment_id)
        try:
            cleaned = rules.clean_rule_input(equipment, request.data)
        except rules.RuleInputError as exc:
            return _input_error(exc)
        rule, result = rules.create_rule(equipment, cleaned, request.user)
        rule = _rule_for(equipment, rule.pk)
        return Response({"rule": rules.serialize_rule(rule), "result": result}, status=status.HTTP_201_CREATED)


class SlotBlockRulePreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, equipment_id: int):
        equipment = _equipment_for(request, equipment_id)
        try:
            cleaned = rules.clean_rule_input(equipment, request.data)
        except rules.RuleInputError as exc:
            return _input_error(exc)
        return Response({"preview": rules.preview_rule(equipment, cleaned)})


class SlotBlockRuleDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, equipment_id: int, rule_id: int):
        equipment = _equipment_for(request, equipment_id)
        return Response({"rule": rules.serialize_rule(_rule_for(equipment, rule_id))})


class SlotBlockRuleRemoveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, equipment_id: int, rule_id: int):
        equipment = _equipment_for(request, equipment_id)
        rule = _rule_for(equipment, rule_id)
        try:
            result = rules.remove_rule(rule, request.user)
        except rules.RuleInputError as exc:
            return _input_error(exc)
        rule = _rule_for(equipment, rule_id)
        return Response({"rule": rules.serialize_rule(rule, with_removal_preview=False), "result": result})
