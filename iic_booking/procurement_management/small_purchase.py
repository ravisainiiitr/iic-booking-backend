"""Small-purchase rule.

A purchase may be made directly (no prior approval workflow) when its total *including GST* is at or below the
department's ``small_purchase_threshold`` (configured; ₹2,000 is only the initial value) and the category and
request type allow small purchases — or when the category is explicitly approval-exempt. Anything else must go
through the approval workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Evaluation:
    eligible: bool
    reason: str
    threshold: Decimal

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "reason": self.reason, "threshold": str(self.threshold)}


def evaluate(cfg, total: Decimal, *, category=None, request_type=None) -> Evaluation:
    threshold = cfg.small_purchase_threshold
    if category is not None and category.approval_exempt:
        return Evaluation(True, "category_exempt", threshold)
    if category is not None and not category.small_purchase_allowed:
        return Evaluation(False, "category_not_allowed", threshold)
    if request_type is not None and not request_type.allow_small_purchase:
        return Evaluation(False, "request_type_not_allowed", threshold)
    if total <= threshold:
        return Evaluation(True, "within_threshold", threshold)
    return Evaluation(False, "above_threshold", threshold)
