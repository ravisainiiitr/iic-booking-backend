"""Optional role assignments for equipment procurement / write-off workflow steps."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from .user import User


class EquipmentSupplyChainRole(models.TextChoices):
    """Designated approvers in the Lab → OIC → Office → Store → HoD chain."""

    OFFICE_SUPERINTENDENT = "OFFICE_SUPERINTENDENT", _("Office Superintendent")
    STORE_IN_CHARGE = "STORE_IN_CHARGE", _("Store In Charge")
    HEAD_OF_DEPARTMENT = "HEAD_OF_DEPARTMENT", _("Head of Department")


class UserEquipmentSupplyChainRole(models.Model):
    """
    When at least one row exists for a given role, only those users (plus Admin) may perform
    that workflow step. If no users are assigned for a role, any admin-panel user may act (legacy).
    """

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="equipment_supply_chain_roles")
    role = models.CharField(max_length=40, choices=EquipmentSupplyChainRole.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="uniq_user_equipment_supply_chain_role"),
        ]
        verbose_name = _("User equipment supply chain role")
        verbose_name_plural = _("User equipment supply chain roles")

    def __str__(self) -> str:
        return f"{self.user_id} — {self.role}"
