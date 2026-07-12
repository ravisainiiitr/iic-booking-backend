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
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import HttpResponse, Http404

from ..models import User, WalletRechargeRequest, WalletRechargeRequestStatus
from ..serializers.wallet_serializer import WalletRechargeRequestApproveSerializer


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


class ApproveRechargeRequestView(View):
    """View for approving wallet recharge requests via web form."""
    
    def get(self, request, request_id):
        """Display approve form."""
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(request, "users/wallet_recharge_request_not_found.html", {
                "error_message": "Wallet recharge request not found.",
                "request_id": request_id,
            }, status=404)
        
        # Show status page if request is not pending
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            status_info = {
                WalletRechargeRequestStatus.APPROVED: {
                    "title": "Request Already Approved",
                    "message": "This recharge request has already been approved.",
                    "color": "#4CAF50",
                    "icon": "✓",
                },
                WalletRechargeRequestStatus.REJECTED: {
                    "title": "Request Already Rejected",
                    "message": "This recharge request has already been rejected.",
                    "color": "#f44336",
                    "icon": "✗",
                },
                WalletRechargeRequestStatus.CANCELLED: {
                    "title": "Request Cancelled",
                    "message": "This recharge request has been cancelled.",
                    "color": "#6c757d",
                    "icon": "⊘",
                },
            }
            info = status_info.get(recharge_request.status, {
                "title": "Request Already Processed",
                "message": "This recharge request has already been processed.",
                "color": "#6c757d",
                "icon": "!",
            })
            
            return render(request, "users/wallet_recharge_request_already_processed.html", {
                "recharge_request": recharge_request,
                "status_title": info["title"],
                "status_message": info["message"],
                "status_color": info["color"],
                "status_icon": info["icon"],
            }, status=404)
        
        context = {
            "recharge_request": recharge_request,
            "action": "approve",
            "action_label": "Approve",
            "button_color": "#4CAF50",
        }
        return render(request, "users/wallet_recharge_action.html", context)
    
    def post(self, request, request_id):
        """Handle approve form submission."""
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(request, "users/wallet_recharge_request_not_found.html", {
                "error_message": "Wallet recharge request not found.",
                "request_id": request_id,
            }, status=404)
        
        # Show status page if request is not pending
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            status_info = {
                WalletRechargeRequestStatus.APPROVED: {
                    "title": "Request Already Approved",
                    "message": "This recharge request has already been approved.",
                    "color": "#4CAF50",
                    "icon": "✓",
                },
                WalletRechargeRequestStatus.REJECTED: {
                    "title": "Request Already Rejected",
                    "message": "This recharge request has already been rejected.",
                    "color": "#f44336",
                    "icon": "✗",
                },
                WalletRechargeRequestStatus.CANCELLED: {
                    "title": "Request Cancelled",
                    "message": "This recharge request has been cancelled.",
                    "color": "#6c757d",
                    "icon": "⊘",
                },
            }
            info = status_info.get(recharge_request.status, {
                "title": "Request Already Processed",
                "message": "This recharge request has already been processed.",
                "color": "#6c757d",
                "icon": "!",
            })
            
            return render(request, "users/wallet_recharge_request_already_processed.html", {
                "recharge_request": recharge_request,
                "status_title": info["title"],
                "status_message": info["message"],
                "status_color": info["color"],
                "status_icon": info["icon"],
            }, status=404)
        
        response_message = request.POST.get('response_message', '').strip()
        
        try:
            recharge_request.approve(response_message)
            
            # Send notification to user
            try:
                from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
                send_wallet_recharge_request_notifications(recharge_request, "APPROVED")
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to send approval notification: {str(e)}")
            
            messages.success(
                request,
                f"Recharge request approved successfully. ₹{recharge_request.amount} has been credited to {recharge_request.department.name if recharge_request.department else 'wallet'}."
            )
            return render(request, "users/wallet_recharge_action_success.html", {
                "recharge_request": recharge_request,
                "action": "approved",
            })
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("users:approve-recharge-request", request_id=request_id)
        except Exception as e:
            messages.error(request, f"Failed to approve request: {str(e)}")
            return redirect("users:approve-recharge-request", request_id=request_id)


class RejectRechargeRequestView(View):
    """View for rejecting wallet recharge requests via web form."""
    
    def get(self, request, request_id):
        """Display reject form."""
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(request, "users/wallet_recharge_request_not_found.html", {
                "error_message": "Wallet recharge request not found.",
                "request_id": request_id,
            }, status=404)
        
        # Show status page if request is not pending
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            status_info = {
                WalletRechargeRequestStatus.APPROVED: {
                    "title": "Request Already Approved",
                    "message": "This recharge request has already been approved.",
                    "color": "#4CAF50",
                    "icon": "✓",
                },
                WalletRechargeRequestStatus.REJECTED: {
                    "title": "Request Already Rejected",
                    "message": "This recharge request has already been rejected.",
                    "color": "#f44336",
                    "icon": "✗",
                },
                WalletRechargeRequestStatus.CANCELLED: {
                    "title": "Request Cancelled",
                    "message": "This recharge request has been cancelled.",
                    "color": "#6c757d",
                    "icon": "⊘",
                },
            }
            info = status_info.get(recharge_request.status, {
                "title": "Request Already Processed",
                "message": "This recharge request has already been processed.",
                "color": "#6c757d",
                "icon": "!",
            })
            
            return render(request, "users/wallet_recharge_request_already_processed.html", {
                "recharge_request": recharge_request,
                "status_title": info["title"],
                "status_message": info["message"],
                "status_color": info["color"],
                "status_icon": info["icon"],
            }, status=404)
        
        context = {
            "recharge_request": recharge_request,
            "action": "reject",
            "action_label": "Reject",
            "button_color": "#f44336",
        }
        return render(request, "users/wallet_recharge_action.html", context)
    
    def post(self, request, request_id):
        """Handle reject form submission."""
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(request, "users/wallet_recharge_request_not_found.html", {
                "error_message": "Wallet recharge request not found.",
                "request_id": request_id,
            }, status=404)
        
        # Show status page if request is not pending
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            status_info = {
                WalletRechargeRequestStatus.APPROVED: {
                    "title": "Request Already Approved",
                    "message": "This recharge request has already been approved.",
                    "color": "#4CAF50",
                    "icon": "✓",
                },
                WalletRechargeRequestStatus.REJECTED: {
                    "title": "Request Already Rejected",
                    "message": "This recharge request has already been rejected.",
                    "color": "#f44336",
                    "icon": "✗",
                },
                WalletRechargeRequestStatus.CANCELLED: {
                    "title": "Request Cancelled",
                    "message": "This recharge request has been cancelled.",
                    "color": "#6c757d",
                    "icon": "⊘",
                },
            }
            info = status_info.get(recharge_request.status, {
                "title": "Request Already Processed",
                "message": "This recharge request has already been processed.",
                "color": "#6c757d",
                "icon": "!",
            })
            
            return render(request, "users/wallet_recharge_request_already_processed.html", {
                "recharge_request": recharge_request,
                "status_title": info["title"],
                "status_message": info["message"],
                "status_color": info["color"],
                "status_icon": info["icon"],
            }, status=404)
        
        response_message = request.POST.get('response_message', '').strip()
        
        if not response_message:
            messages.error(request, "Response message is required for rejection.")
            return redirect("users:reject-recharge-request", request_id=request_id)
        
        try:
            recharge_request.reject(response_message)
            
            # Send notification to user
            try:
                from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
                send_wallet_recharge_request_notifications(recharge_request, "REJECTED")
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to send rejection notification: {str(e)}")
            
            messages.success(request, "Recharge request rejected successfully.")
            return render(request, "users/wallet_recharge_action_success.html", {
                "recharge_request": recharge_request,
                "action": "rejected",
            })
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("users:reject-recharge-request", request_id=request_id)
        except Exception as e:
            messages.error(request, f"Failed to reject request: {str(e)}")
            return redirect("users:reject-recharge-request", request_id=request_id)


approve_recharge_request_view = ApproveRechargeRequestView.as_view()
reject_recharge_request_view = RejectRechargeRequestView.as_view()


class RechargeRequestActionView(View):
    """View for displaying approve/reject options for a wallet recharge request."""
    
    def get(self, request, request_id):
        """Display action page with approve and reject buttons."""
        try:
            recharge_request = WalletRechargeRequest.objects.get(pk=request_id)
        except WalletRechargeRequest.DoesNotExist:
            return render(request, "users/wallet_recharge_request_not_found.html", {
                "error_message": "Wallet recharge request not found.",
                "request_id": request_id,
            }, status=404)
        
        # Show status page if request is not pending
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            status_info = {
                WalletRechargeRequestStatus.APPROVED: {
                    "title": "Request Already Approved",
                    "message": "This recharge request has already been approved.",
                    "color": "#4CAF50",
                    "icon": "✓",
                },
                WalletRechargeRequestStatus.REJECTED: {
                    "title": "Request Already Rejected",
                    "message": "This recharge request has already been rejected.",
                    "color": "#f44336",
                    "icon": "✗",
                },
                WalletRechargeRequestStatus.CANCELLED: {
                    "title": "Request Cancelled",
                    "message": "This recharge request has been cancelled.",
                    "color": "#6c757d",
                    "icon": "⊘",
                },
            }
            info = status_info.get(recharge_request.status, {
                "title": "Request Already Processed",
                "message": "This recharge request has already been processed.",
                "color": "#6c757d",
                "icon": "!",
            })
            
            return render(request, "users/wallet_recharge_request_already_processed.html", {
                "recharge_request": recharge_request,
                "status_title": info["title"],
                "status_message": info["message"],
                "status_color": info["color"],
                "status_icon": info["icon"],
            }, status=404)
        
        from django.urls import reverse
        approve_url = reverse('users:approve-recharge-request', kwargs={'request_id': recharge_request.id})
        reject_url = reverse('users:reject-recharge-request', kwargs={'request_id': recharge_request.id})
        
        context = {
            "recharge_request": recharge_request,
            "approve_url": approve_url,
            "reject_url": reject_url,
        }
        return render(request, "users/wallet_recharge_action_page.html", context)


recharge_request_action_view = RechargeRequestActionView.as_view()

