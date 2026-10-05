from django.urls import path

from iic_booking.equipment.slot_block_rules_views import (
    SlotBlockRuleDetailView,
    SlotBlockRuleListCreateView,
    SlotBlockRulePreviewView,
    SlotBlockRuleRemoveView,
)

urlpatterns = [
    path("", SlotBlockRuleListCreateView.as_view(), name="slot-block-rules"),
    path("preview/", SlotBlockRulePreviewView.as_view(), name="slot-block-rules-preview"),
    path("<int:rule_id>/", SlotBlockRuleDetailView.as_view(), name="slot-block-rules-detail"),
    path("<int:rule_id>/remove/", SlotBlockRuleRemoveView.as_view(), name="slot-block-rules-remove"),
]
