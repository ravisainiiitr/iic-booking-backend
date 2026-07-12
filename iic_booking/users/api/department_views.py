# This file is kept for backward compatibility
# New code should use iic_booking.users.views.department_views
from iic_booking.users.views import (
    department_list,
    department_detail,
    request_organization,
)

__all__ = [
    "department_list",
    "department_detail",
    "request_organization",
]
