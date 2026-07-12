# This file is kept for backward compatibility
# New code should use iic_booking.users.serializers
from iic_booking.users.serializers import (
    DepartmentSerializer,
    DepartmentListSerializer,
)

__all__ = [
    "DepartmentSerializer",
    "DepartmentListSerializer",
]
