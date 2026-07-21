"""Django class-based views for users app."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import QuerySet
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import RedirectView
from django.views.generic import UpdateView
from django.views import View
from django.shortcuts import render, redirect

from iic_booking.communication.utils import get_frontend_absolute_url

from ..models import User, WalletRechargeRequest


class UserDetailView(LoginRequiredMixin, DetailView):
    """Detail view for User model."""

    model = User
    slug_field = "id"
    slug_url_kwarg = "id"


user_detail_view = UserDetailView.as_view()


class UserUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    """Update view for User model."""

    model = User
    fields = ["name"]
    success_message = _("Information successfully updated")

    def get_success_url(self) -> str:
        assert self.request.user.is_authenticated  # type guard
        return self.request.user.get_absolute_url()

    def get_object(self, queryset: QuerySet | None = None) -> User:
        assert self.request.user.is_authenticated  # type guard
        return self.request.user


user_update_view = UserUpdateView.as_view()


class UserRedirectView(LoginRequiredMixin, RedirectView):
    """Redirect view for User model."""

    permanent = False

    def get_redirect_url(self) -> str:
        return reverse("users:detail", kwargs={"pk": self.request.user.pk})


user_redirect_view = UserRedirectView.as_view()


def _frontend_recharge_action_url(recharge_request: WalletRechargeRequest, action: str) -> str:
    """Redirect legacy Django approve/reject URLs to the secure frontend token pages."""
    if not recharge_request.action_token:
        import secrets

        recharge_request.action_token = secrets.token_urlsafe(32)
        recharge_request.save(update_fields=["action_token", "updated_at"])
    return get_frontend_absolute_url(f"/wallet/recharge-action/{recharge_request.action_token}/{action}")


class ApproveRechargeRequestView(View):
    """Legacy backend URL — redirects to frontend token Approve page."""

    def get(self, request, request_id):
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(
                request,
                "users/wallet_recharge_request_not_found.html",
                {
                    "error_message": "Wallet recharge request not found.",
                    "request_id": request_id,
                },
                status=404,
            )
        return redirect(_frontend_recharge_action_url(recharge_request, "approve"))

    def post(self, request, request_id):
        return self.get(request, request_id)


approve_recharge_request_view = ApproveRechargeRequestView.as_view()


class RejectRechargeRequestView(View):
    """Legacy backend URL — redirects to frontend token Reject page."""

    def get(self, request, request_id):
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(
                request,
                "users/wallet_recharge_request_not_found.html",
                {
                    "error_message": "Wallet recharge request not found.",
                    "request_id": request_id,
                },
                status=404,
            )
        return redirect(_frontend_recharge_action_url(recharge_request, "reject"))

    def post(self, request, request_id):
        return self.get(request, request_id)


reject_recharge_request_view = RejectRechargeRequestView.as_view()


class RechargeRequestActionView(View):
    """Legacy overview URL — redirects to frontend token Approve page (status shown there)."""

    def get(self, request, request_id):
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(
                request,
                "users/wallet_recharge_request_not_found.html",
                {
                    "error_message": "Wallet recharge request not found.",
                    "request_id": request_id,
                },
                status=404,
            )
        return redirect(_frontend_recharge_action_url(recharge_request, "approve"))


recharge_request_action_view = RechargeRequestActionView.as_view()
