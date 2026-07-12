# This file is kept for backward compatibility
# New code should use iic_booking.users.views.django_views
# Note: Since views/ is now a package, this file won't be imported when using "from .views import"
# but it's kept here for any direct imports that might reference it
from iic_booking.users.views import (
    user_detail_view,
    user_update_view,
    user_redirect_view,
)

__all__ = [
    "user_detail_view",
    "user_update_view",
    "user_redirect_view",
]
