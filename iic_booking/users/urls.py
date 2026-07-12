from django.urls import path

from .views import user_detail_view
from .views import user_redirect_view
from .views import user_update_view
from .views import approve_recharge_request_view
from .views import reject_recharge_request_view
from .views import recharge_request_action_view

app_name = "users"
urlpatterns = [
    path("~redirect/", view=user_redirect_view, name="redirect"),
    path("~update/", view=user_update_view, name="update"),
    # More specific patterns first to avoid conflicts with generic <int:pk> pattern
    path("wallet/recharge-requests/<int:request_id>/approve/", view=approve_recharge_request_view, name="approve-recharge-request"),
    path("wallet/recharge-requests/<int:request_id>/reject/", view=reject_recharge_request_view, name="reject-recharge-request"),
    path("wallet/recharge-requests/<int:request_id>/", view=recharge_request_action_view, name="recharge-request-action"),
    # Generic pattern last
    path("<int:pk>/", view=user_detail_view, name="detail"),
]
