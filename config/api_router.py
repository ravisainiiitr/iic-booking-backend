from django.conf import settings
from django.urls import path, include
from django.urls.resolvers import URLPattern
from django.urls.resolvers import URLResolver

from config.admin_api import admin_api_router
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from iic_booking.users.api.auth_views import (
    omniport_auth_url,
    omniport_callback,
    login,
    register,
    logout,
    current_user,
    auth_settings,
    profile_me,
    profile_me_avatar,
    user_profile_picture_proxy,
    get_register_user_types,
    get_indian_states,
    search_faculty_for_signup,
    verify_email,
    self_verify,
    resend_verification_email,
    request_login_otp,
    verify_login_otp,
    request_forgot_password_otp,
    verify_forgot_password_otp_and_set_password,
)
from iic_booking.users.views import (
    department_list,
    department_detail,
    request_organization,
    UserViewSet,
)
from iic_booking.users.api.user_group_views import (
    user_group_list_create,
    user_group_detail,
    user_group_members,
    user_group_equipment,
)
from iic_booking.users.api.user_roles_views import (
    check_admin as user_roles_check_admin,
    user_roles_list,
)
from iic_booking.users.api.wallet_views import (
    get_wallet,
    get_wallet_balance,
    equipment_department_wallet_balance,
    get_wallet_transactions,
    get_departments_for_recharge,
    transfer_between_sub_wallets,
    get_sub_wallet_transactions,
    get_faculty_by_email,
    search_faculty_by_name,
    request_wallet_join,
    get_my_wallet_requests,
    approve_wallet_join_request,
    wallet_join_email_action,
    reject_wallet_join_request,
    cancel_wallet_join_request,
    remove_student_from_wallet,
    delete_wallet_join_request,
    bulk_delete_wallet_join_requests,
    resend_wallet_join_request_notification,
    create_razorpay_order,
    verify_razorpay_payment,
    send_user_otp_for_recharge,
    create_wallet_recharge_request,
    approve_wallet_recharge_request,
    reject_wallet_recharge_request,
    get_my_recharge_requests,
    get_wallet_recharge_pipeline_requests,
    cancel_wallet_recharge_request,
    resend_wallet_recharge_notification,
    send_sric_wallet_recharge_notification,
    wallet_recharge_action_detail,
    wallet_recharge_action_approve,
    wallet_recharge_action_reject,
    parse_wallet_recharge_file,
    process_wallet_recharge_rows,
    wallet_recharge_parse_entries,
    apply_wallet_recharge_parse_entry,
    wallet_recharge_target_user_projects,
    create_wallet_recharge_request_from_unmatched_parse_row,
    admin_wallet_eligible_users,
    admin_manual_wallet_recharge,
    wallet_imap_list_emails,
    wallet_imap_fetch_and_parse,
    wallet_imap_email_attachments,
    wallet_imap_download_attachment,
    wallet_imap_delete_email_if_processed,
    wallet_bank_details,
    create_wallet_withdrawal_request,
    get_my_withdrawal_requests,
    cancel_wallet_withdrawal_request,
    faculty_wallet_expense_report,
    wallet_credit_facility_settings_view,
    wallet_credit_facility_offer_for_recharge_view,
    wallet_credit_facility_my_status_view,
    wallet_student_recharge_settings_view,
    legacy_wallet_balance_lookup,
    legacy_wallet_balance_list,
)
from iic_booking.users.api.wallet_peer_transfer_views import (
    wallet_peer_transfer_eligible_recipients,
    wallet_peer_transfer_send_otp,
    wallet_peer_transfer_confirm,
    wallet_peer_transfer_history,
    wallet_peer_transfer_source_departments,
)
from iic_booking.users.api.project_views import (
    project_list,
    project_detail,
    project_delete,
)
from iic_booking.users.api.billing_views import external_billing_profile_me
from iic_booking.users.api.payment_views import (
    sbiepay_initiate,
    sbiepay_return_success,
    sbiepay_return_failure,
    sbiepay_transaction_status,
    submit_payment_utr,
    submit_wallet_recharge_receipt,
    finance_payment_receipts_list,
    finance_payment_receipt_process,
)
from iic_booking.users.api.sric_api_views import (
    sric_transfer_requests_list,
    sric_transfer_request_detail,
    sric_transfer_request_complete,
    sric_transfer_request_reject,
)
from iic_booking.users.api.sync_agent_views import (
    sync_agent_register,
    sync_agent_authenticate,
    sync_agent_refresh,
)
from iic_booking.equipment.print_3d_views import (
    equipment_analyze_stl,
    equipment_print_materials,
    print_analysis_detail,
    print_analysis_batch_detail,
    recalculate_print_analysis,
    recalculate_print_analysis_batch,
    download_print_analysis_stl,
    presign_print_analysis_stl,
    update_booking_print_actuals,
)
from iic_booking.equipment.api_views import (
    equipment_list,
    equipment_catalog_departments,
    equipment_category_list,
    equipment_detail,
    equipment_image_proxy,
    equipment_form_choices,
    equipment_calculate,
    equipment_ratings,
    icpms_min_standards_cover,
    icpms_available_standards,
    icpms_standards_full_list,
    proforma_invoice_calculate,
    equipment_daily_slots,
    book_equipment,
    temporary_oic_list_oic_users,
    temporary_oic_my_equipments,
    temporary_oic_create,
    temporary_oic_list_mine,
    temporary_oic_cancel,
    temporary_oic_update,
    list_bookings,
    approaching_sample_submission_deadlines,
    booking_stats,
    lab_operator_dashboard,
    operator_leave_summary,
    operator_leave_requests,
    operator_leave_request_resume,
    oic_leave_requests_pending,
    oic_leave_requests_approved,
    oic_leave_request_coverage_options,
    oic_leave_request_approve_with_coverage,
    oic_leave_request_coverages,
    oic_leave_request_set_coverages,
    oic_leave_request_approve,
    oic_leave_request_reject,
    oic_leave_request_resume,
    team_calendar_department_leaves,
    booking_results,
    booking_results_download,
    update_booking_istem_fbr,
    review_booking_istem_fbr,
    complete_booking,
    refund_booking,
    mark_booking_not_utilized,
    absent_booking,
    extend_booking_operator_absent_hold,
    booking_maintenance_disruption,
    booking_other_disruption,
    reschedule_booking,
    cancel_booking,
    partial_cancel_preview,
    user_cancel_booking,
    user_reschedule_booking,
    list_booking_events,
    create_booking_event_comment,
    booking_sample_trace,
    set_booking_sample_status,
    ensure_booking_results_folder,
    set_sample_trace_reply,
    set_booking_return_shipping_tracking,
    update_booking_input_values,
    update_booking_atmosphere_sensitive_sample,
    rate_booking,
    remove_booking_rating,
    process_charge_recalculation_refund,
    process_charge_recalculation_pay_now,
    get_repeat_sample_info,
    request_repeat_sample,
    enable_repeat_sample,
    get_repeat_sample_eligibility,
    create_repeat_booking,
    list_repeat_sample_requests,
    approve_repeat_sample_request,
    reject_repeat_sample_request,
    bulk_email_recipients,
    send_bulk_email,
    log_no_slot_allocation,
    log_booking_attempt,
    list_booking_attempt_logs,
    get_my_unsuccessful_booking_attempts,
    list_my_waitlist_entries,
    cancel_my_waitlist_entry,
    get_booking_attempt_log_quota_breakdown,
    delete_booking_attempt_log,
    create_urgent_booking_request,
    get_approved_urgent_history,
    list_my_urgent_booking_requests,
    list_urgent_booking_requests,
    get_urgent_request_detail,
    get_no_slot_log_for_user,
    update_urgent_booking_request,
    wallet_approve_urgent_booking_request,
    get_urgent_request_evidence,
    urgent_hold_expiry_config,
    list_urgent_requests_pending_wallet_approval,
    list_urgent_requests_wallet,
    list_semesters,
    create_equipment_nomination,
    list_my_nominations_as_supervisor,
    list_my_nominations_as_student,
    submit_nomination_resume,
    get_nomination_resume,
    revoke_equipment_nomination,
    list_equipment_nominations_admin,
    approve_equipment_nomination,
    reject_equipment_nomination,
    create_ta_nomination_call,
    list_ta_nomination_calls,
    list_open_ta_calls_faculty,
    my_reward_summary,
    my_reward_ledger,
    allocate_ta_assignment,
    list_ta_assignments,
    respond_ta_assignment,
    cancel_ta_assignment,
    create_ta_duty_log,
    list_ta_duty_logs,
    verify_ta_duty_log,
    reject_ta_duty_log,
    reward_config_view,
    oic_equipment_accessories_list,
    oic_toggle_equipment_accessory,
    oic_toggle_equipment_additional_accessory,
    oic_print_materials,
    oic_print_material_detail,
    oic_equipment_group_quotas,
    oic_multi_mode_list,
    oic_multi_mode_schedule_create,
    oic_multi_mode_schedule_detail,
    admin_adjust_reward_points,
    inventory_items_list,
    equipment_inventory_stock,
    inventory_requests,
    inventory_request_decide,
    inventory_request_issue,
    inventory_stock_add,
    procurement_requests,
    procurement_request_oic_endorse,
    procurement_request_office_verify,
    procurement_request_store_approve,
    procurement_request_head_approve,
    procurement_request_mark_purchase_complete,
    procurement_request_mark_office_seen,
    equipment_lifecycle_equipment_choices,
    equipment_lifecycle_detail,
    equipment_amc_contracts,
    equipment_expenses,
    equipment_write_off_requests,
    equipment_write_off_office_action,
    equipment_write_off_store_action,
    equipment_write_off_head_action,
    equipment_write_off_execute,
)
from iic_booking.equipment.api_document_views import (
    booking_invoice_pdf,
    booking_shipping_label_pdf,
    booking_return_shipping_label_pdf,
    equipment_proforma_invoice_pdf,
    proforma_invoice_pdf_download,
)
from iic_booking.equipment.equipment_addition_requests import (
    equipment_addition_form_choices,
    equipment_addition_request_create,
    equipment_addition_request_list,
    equipment_addition_request_detail,
    equipment_addition_request_approve,
    equipment_addition_request_reject,
)
from iic_booking.communication.api_views import (
    get_notifications,
    mark_notification_as_read,
    mark_all_notifications_as_read,
    delete_notification,
    notice_list,
    notice_detail,
    list_inbox_folders,
    fetch_inbox_emails,
)
from iic_booking.support.api_views import (
    ticket_list,
    ticket_detail,
    ticket_comment_create,
    ticket_comments_list,
    ticket_type_list,
    ticket_attachment,
    ticket_events_list,
    ticket_assignees_search,
    chat_agent,
)
from iic_booking.support.portal_feedback_views import (
    portal_feedback_mine,
    portal_feedback_admin_list,
)
from iic_booking.cms.views import menu_list, home_page_content, hero_slides, page_by_slug, site_stats

router = DefaultRouter() if settings.DEBUG else SimpleRouter()
router.register("users", UserViewSet, basename="user")


def get_all_urls(urlpatterns, prefix=""):
    """Recursively extract all URL patterns."""
    routes = []
    for pattern in urlpatterns:
        if isinstance(pattern, URLPattern):
            url_path = prefix + str(pattern.pattern)
            # Clean up the pattern string - remove regex anchors
            url_path = url_path.replace("^", "").replace("$", "")
            routes.append({
                "path": url_path,
                "name": pattern.name or "",
            })
        elif isinstance(pattern, URLResolver):
            # Recursively get URLs from included patterns
            routes.extend(get_all_urls(pattern.url_patterns, prefix + str(pattern.pattern)))
    return routes


def create_api_root_view(urlpatterns_list):
    """Create an API root view that lists all routes."""
    @api_view(["GET"])
    def api_root(request):
        """List all available API routes."""
        import logging

        logger = logging.getLogger(__name__)
        try:
            all_routes = []

            # Get routes from router
            for url_pattern in router.urls:
                if isinstance(url_pattern, URLPattern):
                    url_path = str(url_pattern.pattern).replace("^", "").replace("$", "")
                    all_routes.append({
                        "path": url_path or "",
                        "name": getattr(url_pattern, "name", None) or "",
                    })

            # Get routes from urlpatterns
            all_routes.extend(get_all_urls(urlpatterns_list, ""))

            # Remove duplicates based on path and name
            seen = set()
            unique_routes = []
            for route in all_routes:
                path_val = route.get("path", "")
                name_val = route.get("name", "")
                route_key = (path_val, name_val)
                if route_key not in seen:
                    seen.add(route_key)
                    unique_routes.append({"path": path_val, "name": name_val})

            return Response(sorted(unique_routes, key=lambda x: str(x.get("path", ""))))
        except Exception as exc:
            logger.exception("API root view failed: %s", exc)
            return Response(
                {"error": "Internal server error", "detail": str(exc) if __debug__ else None},
                status=500,
            )
    return api_root


app_name = "api"
urlpatterns = router.urls + [
    # Inbox emails (IMAP) - staff only - register early to avoid 404
    path("inbox-emails/", fetch_inbox_emails, name="inbox-emails"),
    path("inbox-folders/", list_inbox_folders, name="inbox-folders"),
    path("auth/login/", login, name="login"),
    # Department Sync Agent (DSA) — paths without trailing slash match DSA PortalClient.
    path("sync-agent/register", sync_agent_register, name="sync-agent-register"),
    path("sync-agent/register/", sync_agent_register, name="sync-agent-register-slash"),
    path("sync-agent/authenticate", sync_agent_authenticate, name="sync-agent-authenticate"),
    path("sync-agent/authenticate/", sync_agent_authenticate, name="sync-agent-authenticate-slash"),
    path("sync-agent/refresh", sync_agent_refresh, name="sync-agent-refresh"),
    path("sync-agent/refresh/", sync_agent_refresh, name="sync-agent-refresh-slash"),
    path("auth/logout/", logout, name="logout"),
    path("auth/register/", register, name="register"),
    path("auth/register/user-types/", get_register_user_types, name="register-user-types"),
    path("auth/register/indian-states/", get_indian_states, name="register-indian-states"),
    path("auth/register/search-faculty/", search_faculty_for_signup, name="register-search-faculty"),
    path("auth/verify-email/<str:uidb64>/<str:token>/", verify_email, name="verify-email"),
    path("auth/self-verify/<str:uidb64>/<str:token>/", self_verify, name="self-verify"),
    path("auth/resend-verification/", resend_verification_email, name="resend-verification"),
    path("auth/login/request-otp/", request_login_otp, name="auth-request-login-otp"),
    path("auth/login/verify-otp/", verify_login_otp, name="auth-verify-login-otp"),
    path("auth/forgot-password/request-otp/", request_forgot_password_otp, name="auth-request-forgot-password-otp"),
    path("auth/forgot-password/verify-otp-and-set-password/", verify_forgot_password_otp_and_set_password, name="auth-verify-forgot-password-otp-set-password"),

    path("auth/user/", current_user, name="current-user"),
    path("auth/settings/", auth_settings, name="auth-settings"),
    
    path("auth/omniport/authorize/", omniport_auth_url, name="omniport-auth-url"),
    path("auth/omniport/callback/", omniport_callback, name="omniport-callback"),
    path("profiles/me/", profile_me, name="profile-me"),
    path("profiles/me/avatar/", profile_me_avatar, name="profile-me-avatar"),
    path("profiles/me/external-billing/", external_billing_profile_me, name="external-billing-profile-me"),
    path("users/<int:user_id>/profile-picture/", user_profile_picture_proxy, name="user-profile-picture-proxy"),

    # Department endpoints
    path("departments/", department_list, name="department-list"),
    path("departments/request-organization/", request_organization, name="department-request-organization"),
    path("departments/<int:pk>/", department_detail, name="department-detail"),

    # User roles
    path("user-roles/", user_roles_list, name="user-roles-list"),
    path("user-roles/check-admin/", user_roles_check_admin, name="user-roles-check-admin"),

    # User groups (equipment visibility)
    path("user-groups/", user_group_list_create, name="user-group-list-create"),
    path("user-groups/<int:pk>/", user_group_detail, name="user-group-detail"),
    path("user-groups/<int:pk>/members/", user_group_members, name="user-group-members"),
    path("user-groups/<int:pk>/equipment/", user_group_equipment, name="user-group-equipment"),

    # Wallet endpoints
    path("wallet/", get_wallet, name="wallet"),
    path("wallet/faculty-expense-report/", faculty_wallet_expense_report, name="wallet-faculty-expense-report"),
    path("wallet/balance/", get_wallet_balance, name="wallet-balance"),
    path(
        "wallet/equipment-department-balance/",
        equipment_department_wallet_balance,
        name="wallet-equipment-department-balance",
    ),
    path("wallet/transactions/", get_wallet_transactions, name="wallet-transactions"),
    path("wallet/bank-details/", wallet_bank_details, name="wallet-bank-details"),
    path("wallet/departments-for-recharge/", get_departments_for_recharge, name="wallet-departments-for-recharge"),
    path("wallet/transfer-between-sub-wallets/", transfer_between_sub_wallets, name="wallet-transfer-between-sub-wallets"),
    path(
        "wallet/peer-transfer/source-departments/",
        wallet_peer_transfer_source_departments,
        name="wallet-peer-transfer-source-departments",
    ),
    path(
        "wallet/peer-transfer/eligible-recipients/",
        wallet_peer_transfer_eligible_recipients,
        name="wallet-peer-transfer-eligible-recipients",
    ),
    path(
        "wallet/peer-transfer/send-otp/",
        wallet_peer_transfer_send_otp,
        name="wallet-peer-transfer-send-otp",
    ),
    path(
        "wallet/peer-transfer/confirm/",
        wallet_peer_transfer_confirm,
        name="wallet-peer-transfer-confirm",
    ),
    path(
        "wallet/peer-transfer/history/",
        wallet_peer_transfer_history,
        name="wallet-peer-transfer-history",
    ),
    path("wallet/sub-wallets/<int:department_id>/transactions/", get_sub_wallet_transactions, name="wallet-sub-wallet-transactions"),
    path("wallet/withdrawal-request/", create_wallet_withdrawal_request, name="wallet-withdrawal-request-create"),
    path("wallet/withdrawal-requests/", get_my_withdrawal_requests, name="wallet-withdrawal-requests"),
    path("wallet/withdrawal-requests/<int:request_id>/cancel/", cancel_wallet_withdrawal_request, name="wallet-withdrawal-request-cancel"),

    # Razorpay payment endpoints (legacy — prefer SBIePay)
    path("wallet/razorpay/create-order/", create_razorpay_order, name="wallet-razorpay-create-order"),
    path("wallet/razorpay/verify-payment/", verify_razorpay_payment, name="wallet-razorpay-verify-payment"),

    # SBIePay payment gateway
    path("payments/sbiepay/initiate/", sbiepay_initiate, name="sbiepay-initiate"),
    path("payments/sbiepay/success/", sbiepay_return_success, name="sbiepay-success"),
    path("payments/sbiepay/failure/", sbiepay_return_failure, name="sbiepay-failure"),
    path("payments/sbiepay/status/", sbiepay_transaction_status, name="sbiepay-status"),
    path("payments/utr/submit/", submit_payment_utr, name="payment-utr-submit"),
    path(
        "payments/wallet-recharge-receipt/",
        submit_wallet_recharge_receipt,
        name="wallet-recharge-receipt-submit",
    ),
    path("finance/payment-receipts/", finance_payment_receipts_list, name="finance-payment-receipts"),
    path(
        "finance/payment-receipts/<int:receipt_id>/process/",
        finance_payment_receipt_process,
        name="finance-payment-receipt-process",
    ),

    # SRIC office integration API
    path("integrations/sric/transfer-requests/", sric_transfer_requests_list, name="sric-transfer-requests"),
    path(
        "integrations/sric/transfer-requests/<int:transfer_id>/",
        sric_transfer_request_detail,
        name="sric-transfer-request-detail",
    ),
    path(
        "integrations/sric/transfer-requests/<int:transfer_id>/complete/",
        sric_transfer_request_complete,
        name="sric-transfer-request-complete",
    ),
    path(
        "integrations/sric/transfer-requests/<int:transfer_id>/reject/",
        sric_transfer_request_reject,
        name="sric-transfer-request-reject",
    ),
    
    # Wallet join request endpoints
    path("wallet/faculty-by-email/", get_faculty_by_email, name="wallet-faculty-by-email"),
    path("wallet/search-faculty/", search_faculty_by_name, name="wallet-search-faculty"),
    path("wallet/join-request/", request_wallet_join, name="wallet-join-request"),
    path("wallet/join-requests/", get_my_wallet_requests, name="wallet-join-requests"),
    path("wallet/join-requests/bulk-delete/", bulk_delete_wallet_join_requests, name="wallet-join-requests-bulk-delete"),
    path("wallet/join-requests/<int:request_id>/approve/", approve_wallet_join_request, name="wallet-join-request-approve"),
    path("wallet/join-requests/<int:request_id>/email-action/<str:action>/", wallet_join_email_action, name="wallet-join-request-email-action"),
    path("wallet/join-requests/<int:request_id>/reject/", reject_wallet_join_request, name="wallet-join-request-reject"),
    path("wallet/join-requests/<int:request_id>/cancel/", cancel_wallet_join_request, name="wallet-join-request-cancel"),
    path("wallet/join-requests/<int:request_id>/remove/", remove_student_from_wallet, name="wallet-join-request-remove"),
    path("wallet/join-requests/<int:request_id>/delete/", delete_wallet_join_request, name="wallet-join-request-delete"),
    path("wallet/join-requests/<int:request_id>/resend-notification/", resend_wallet_join_request_notification, name="wallet-join-request-resend-notification"),
    
    # Wallet recharge request endpoints
    path("wallet/credit-facility/settings/", wallet_credit_facility_settings_view, name="wallet-credit-facility-settings"),
    path(
        "wallet/student-recharge/settings/",
        wallet_student_recharge_settings_view,
        name="wallet-student-recharge-settings",
    ),
    path(
        "wallet/credit-facility/offer-for-recharge/",
        wallet_credit_facility_offer_for_recharge_view,
        name="wallet-credit-facility-offer-for-recharge",
    ),
    path("wallet/credit-facility/my-status/", wallet_credit_facility_my_status_view, name="wallet-credit-facility-my-status"),
    path("wallet/recharge-request/send-otp/", send_user_otp_for_recharge, name="wallet-recharge-request-send-otp"),
    path("wallet/recharge-request/", create_wallet_recharge_request, name="wallet-recharge-request-create"),
    path("wallet/recharge-requests/pipeline/", get_wallet_recharge_pipeline_requests, name="wallet-recharge-requests-pipeline"),
    path("wallet/recharge-requests/", get_my_recharge_requests, name="wallet-recharge-requests"),
    path("wallet/recharge-action/<str:token>/", wallet_recharge_action_detail, name="wallet-recharge-action-detail"),
    path("wallet/recharge-action/<str:token>/approve/", wallet_recharge_action_approve, name="wallet-recharge-action-approve"),
    path("wallet/recharge-action/<str:token>/reject/", wallet_recharge_action_reject, name="wallet-recharge-action-reject"),
    path("wallet/recharge-requests/<int:request_id>/approve/", approve_wallet_recharge_request, name="wallet-recharge-request-approve"),
    path("wallet/recharge-requests/<int:request_id>/reject/", reject_wallet_recharge_request, name="wallet-recharge-request-reject"),
    path("wallet/recharge-requests/<int:request_id>/cancel/", cancel_wallet_recharge_request, name="wallet-recharge-request-cancel"),
    path("wallet/recharge-requests/<int:request_id>/resend-notification/", resend_wallet_recharge_notification, name="wallet-recharge-request-resend-notification"),
    path(
        "wallet/recharge-requests/<int:request_id>/send-sric/",
        send_sric_wallet_recharge_notification,
        name="wallet-recharge-request-send-sric",
    ),
    path("wallet/parse-recharge-file/", parse_wallet_recharge_file, name="wallet-parse-recharge-file"),
    path("wallet/legacy-wallet/balance/", legacy_wallet_balance_lookup, name="wallet-legacy-wallet-balance"),
    path("wallet/legacy-wallet/balances/", legacy_wallet_balance_list, name="wallet-legacy-wallet-balance-list"),
    path("wallet/process-recharge-rows/", process_wallet_recharge_rows, name="wallet-process-recharge-rows"),
    path("wallet/recharge-parse-entries/", wallet_recharge_parse_entries, name="wallet-recharge-parse-entries"),
    path("wallet/recharge-parse-entry-apply/", apply_wallet_recharge_parse_entry, name="wallet-recharge-parse-entry-apply"),
    path(
        "wallet/recharge-target-user-projects/",
        wallet_recharge_target_user_projects,
        name="wallet-recharge-target-user-projects",
    ),
    path(
        "wallet/recharge-request-from-unmatched-parse-row/",
        create_wallet_recharge_request_from_unmatched_parse_row,
        name="wallet-recharge-request-from-unmatched-parse-row",
    ),
    path("wallet/admin-eligible-users/", admin_wallet_eligible_users, name="wallet-admin-eligible-users"),
    path("wallet/admin-manual-recharge/", admin_manual_wallet_recharge, name="wallet-admin-manual-recharge"),
    path("wallet/imap-list-emails/", wallet_imap_list_emails, name="wallet-imap-list-emails"),
    path("wallet/imap-fetch-and-parse/", wallet_imap_fetch_and_parse, name="wallet-imap-fetch-and-parse"),
    path("wallet/imap-email-attachments/", wallet_imap_email_attachments, name="wallet-imap-email-attachments"),
    path("wallet/imap-download-attachment/", wallet_imap_download_attachment, name="wallet-imap-download-attachment"),
    path(
        "wallet/imap-delete-email-if-processed/",
        wallet_imap_delete_email_if_processed,
        name="wallet-imap-delete-email-if-processed",
    ),
    
    # Project endpoints
    path("projects/", project_list, name="project-list"),  # GET: List projects, POST: Create project
    path("projects/<int:project_id>/", project_detail, name="project-detail"),  # GET, PATCH, PUT: Get/Update project
    path("projects/<int:project_id>/delete/", project_delete, name="project-delete"),  # DELETE

    # Equipment endpoints
    path("equipments/", equipment_list, name="equipment-list"),
    path("equipments/catalog-departments/", equipment_catalog_departments, name="equipment-catalog-departments"),

    # Public equipment addition proposals (admin approves before create)
    path(
        "equipment-addition-requests/form-choices/",
        equipment_addition_form_choices,
        name="equipment-addition-form-choices",
    ),
    path(
        "equipment-addition-requests/",
        equipment_addition_request_create,
        name="equipment-addition-request-create",
    ),
    path(
        "admin/equipment-addition-requests/",
        equipment_addition_request_list,
        name="admin-equipment-addition-request-list",
    ),
    path(
        "admin/equipment-addition-requests/<int:pk>/",
        equipment_addition_request_detail,
        name="admin-equipment-addition-request-detail",
    ),
    path(
        "admin/equipment-addition-requests/<int:pk>/approve/",
        equipment_addition_request_approve,
        name="admin-equipment-addition-request-approve",
    ),
    path(
        "admin/equipment-addition-requests/<int:pk>/reject/",
        equipment_addition_request_reject,
        name="admin-equipment-addition-request-reject",
    ),
    path("equipment-categories/", equipment_category_list, name="equipment-category-list"),
    path("equipments/<int:pk>/image/", equipment_image_proxy, name="equipment-image-proxy"),
    path("equipments/<int:pk>/calculate/", equipment_calculate, name="equipment-calculate"),
    path("equipments/<int:pk>/print-materials/", equipment_print_materials, name="equipment-print-materials"),
    path("equipments/<int:pk>/analyze-stl/", equipment_analyze_stl, name="equipment-analyze-stl"),
    path("print-analyses/<uuid:analysis_id>/", print_analysis_detail, name="print-analysis-detail"),
    path("print-analyses/<uuid:analysis_id>/stl/", download_print_analysis_stl, name="print-analysis-stl-download"),
    path("print-analyses/<uuid:analysis_id>/stl-presign/", presign_print_analysis_stl, name="print-analysis-stl-presign"),
    path(
        "print-analyses/<uuid:analysis_id>/recalculate/",
        recalculate_print_analysis,
        name="print-analysis-recalculate",
    ),
    path(
        "print-analysis-batches/<uuid:batch_id>/",
        print_analysis_batch_detail,
        name="print-analysis-batch-detail",
    ),
    path(
        "print-analysis-batches/<uuid:batch_id>/recalculate/",
        recalculate_print_analysis_batch,
        name="print-analysis-batch-recalculate",
    ),
    path("equipments/<int:equipment_id>/ratings/", equipment_ratings, name="equipment-ratings"),
    path("icpms/standards/min-cover/", icpms_min_standards_cover, name="icpms-min-standards-cover"),
    path("icpms/standards/available/", icpms_available_standards, name="icpms-available-standards"),
    path("icpms/standards/full/", icpms_standards_full_list, name="icpms-standards-full-list"),
    path("proforma-invoice/calculate/", proforma_invoice_calculate, name="proforma-invoice-calculate"),
    path("proforma-invoice/download.pdf", proforma_invoice_pdf_download, name="proforma-invoice-pdf-download"),
    path("bookings/<int:booking_id>/invoice.pdf", booking_invoice_pdf, name="booking-invoice-pdf"),
    path("bookings/<int:booking_id>/shipping-label.pdf", booking_shipping_label_pdf, name="booking-shipping-label-pdf"),
    path("bookings/<int:booking_id>/return-shipping-label.pdf", booking_return_shipping_label_pdf, name="booking-return-shipping-label-pdf"),
    path("equipments/<int:equipment_id>/proforma-invoice.pdf", equipment_proforma_invoice_pdf, name="equipment-proforma-invoice-pdf"),
    path("equipments/<int:pk>/slots/", equipment_daily_slots, name="equipment-daily-slots"),
    path("equipments/<int:pk>/book/", book_equipment, name="book-equipment"),
    path("equipments/<int:pk>/", equipment_detail, name="equipment-detail"),
    path("equipments/temporary-oic/oic-users/", temporary_oic_list_oic_users, name="temporary-oic-list-oic-users"),
    path("equipments/temporary-oic/my-equipments/", temporary_oic_my_equipments, name="temporary-oic-my-equipments"),
    path("equipments/temporary-oic/", temporary_oic_create, name="temporary-oic-create"),
    path("equipments/temporary-oic/mine/", temporary_oic_list_mine, name="temporary-oic-list-mine"),
    path("equipments/temporary-oic/<int:delegation_id>/", temporary_oic_update, name="temporary-oic-update"),
    path("equipments/temporary-oic/<int:delegation_id>/cancel/", temporary_oic_cancel, name="temporary-oic-cancel"),
    
    # Booking endpoints
    path("bookings/", list_bookings, name="list-bookings"),
    path(
        "bookings/approaching-sample-submission/",
        approaching_sample_submission_deadlines,
        name="approaching-sample-submission",
    ),
    path("bookings/stats/", booking_stats, name="booking-stats"),
    path("bookings/lab-operator-dashboard/", lab_operator_dashboard, name="lab-operator-dashboard"),
    path("operator/leave/summary/", operator_leave_summary, name="operator-leave-summary"),
    path("operator/leave/requests/", operator_leave_requests, name="operator-leave-requests"),
    path("operator/leave/requests/<int:leave_id>/resume/", operator_leave_request_resume, name="operator-leave-request-resume"),
    path("oic/leave/requests/pending/", oic_leave_requests_pending, name="oic-leave-requests-pending"),
    path("oic/leave/requests/approved/", oic_leave_requests_approved, name="oic-leave-requests-approved"),
    path("oic/leave/requests/<int:leave_id>/coverage-options/", oic_leave_request_coverage_options, name="oic-leave-request-coverage-options"),
    path("oic/leave/requests/<int:leave_id>/approve-with-coverage/", oic_leave_request_approve_with_coverage, name="oic-leave-request-approve-with-coverage"),
    path("oic/leave/requests/<int:leave_id>/coverages/", oic_leave_request_coverages, name="oic-leave-request-coverages"),
    path("oic/leave/requests/<int:leave_id>/set-coverages/", oic_leave_request_set_coverages, name="oic-leave-request-set-coverages"),
    path("oic/leave/requests/<int:leave_id>/approve/", oic_leave_request_approve, name="oic-leave-request-approve"),
    path("oic/leave/requests/<int:leave_id>/reject/", oic_leave_request_reject, name="oic-leave-request-reject"),
    path("oic/leave/requests/<int:leave_id>/resume/", oic_leave_request_resume, name="oic-leave-request-resume"),
    path("team-calendar/department/", team_calendar_department_leaves, name="team-calendar-department"),
    path("bookings/<int:booking_id>/results/", booking_results, name="booking-results"),
    path("bookings/<int:booking_id>/results/download/", booking_results_download, name="booking-results-download"),
    path("bookings/<int:booking_id>/istem-fbr/", update_booking_istem_fbr, name="booking-istem-fbr-update"),
    path("bookings/<int:booking_id>/istem-fbr/review/", review_booking_istem_fbr, name="booking-istem-fbr-review"),
    path("bookings/<int:booking_id>/complete/", complete_booking, name="complete-booking"),
    path("bookings/<int:booking_id>/refund/", refund_booking, name="refund-booking"),
    path("bookings/<int:booking_id>/mark-not-utilized/", mark_booking_not_utilized, name="mark-booking-not-utilized"),
    path("bookings/<int:booking_id>/absent/", absent_booking, name="absent-booking"),
    path(
        "bookings/<int:booking_id>/extend-operator-absent-hold/",
        extend_booking_operator_absent_hold,
        name="extend-booking-operator-absent-hold",
    ),
    path(
        "bookings/<int:booking_id>/maintenance-disruption/",
        booking_maintenance_disruption,
        name="booking-maintenance-disruption",
    ),
    path(
        "bookings/<int:booking_id>/other-disruption/",
        booking_other_disruption,
        name="booking-other-disruption",
    ),
    path("bookings/<int:booking_id>/reschedule/", reschedule_booking, name="reschedule-booking"),
    path("bookings/<int:booking_id>/cancel/", cancel_booking, name="cancel-booking"),
    path(
        "bookings/<int:booking_id>/partial-cancel-preview/",
        partial_cancel_preview,
        name="partial-cancel-preview",
    ),
    # User booking management endpoints (users can manage their own bookings)
    path("bookings/<int:booking_id>/user-cancel/", user_cancel_booking, name="user-cancel-booking"),
    path("bookings/<int:booking_id>/user-reschedule/", user_reschedule_booking, name="user-reschedule-booking"),
    
    # Booking event history endpoints
    path("bookings/<int:booking_id>/events/", list_booking_events, name="list-booking-events"),
    path("bookings/<int:booking_id>/events/comment/", create_booking_event_comment, name="create-booking-event-comment"),
    path("bookings/<int:booking_id>/sample-trace/", booking_sample_trace, name="booking-sample-trace"),
    path("bookings/<int:booking_id>/sample-trace/set/", set_booking_sample_status, name="set-booking-sample-status"),
    path("bookings/<int:booking_id>/ensure-results-folder/", ensure_booking_results_folder, name="ensure-booking-results-folder"),
    path("bookings/<int:booking_id>/sample-trace/<int:event_id>/reply/", set_sample_trace_reply, name="set-sample-trace-reply"),
    path("bookings/<int:booking_id>/return-shipping-tracking/", set_booking_return_shipping_tracking, name="booking-return-shipping-tracking"),
    path("bookings/<int:booking_id>/input-values/", update_booking_input_values, name="update-booking-input-values"),
    path(
        "bookings/<int:booking_id>/atmosphere-sensitive-sample/",
        update_booking_atmosphere_sensitive_sample,
        name="update-booking-atmosphere-sensitive-sample",
    ),
    path("bookings/<int:booking_id>/print-actuals/", update_booking_print_actuals, name="update-booking-print-actuals"),
    path("bookings/<int:booking_id>/rate/", rate_booking, name="rate-booking"),
    path("bookings/<int:booking_id>/rating/remove/", remove_booking_rating, name="remove-booking-rating"),
    path("bookings/<int:booking_id>/process-charge-recalculation-refund/", process_charge_recalculation_refund, name="process-charge-recalculation-refund"),
    path("bookings/<int:booking_id>/process-charge-recalculation-pay-now/", process_charge_recalculation_pay_now, name="process-charge-recalculation-pay-now"),
    path("bookings/<int:booking_id>/repeat-sample-info/", get_repeat_sample_info, name="repeat-sample-info"),
    path("bookings/<int:booking_id>/request-repeat-sample/", request_repeat_sample, name="request-repeat-sample"),
    path("bookings/<int:booking_id>/enable-repeat-sample/", enable_repeat_sample, name="enable-repeat-sample"),
    path("bookings/<int:booking_id>/repeat-sample-eligibility/", get_repeat_sample_eligibility, name="repeat-sample-eligibility"),
    path("bookings/<int:booking_id>/create-repeat-booking/", create_repeat_booking, name="create-repeat-booking"),
    path("repeat-sample-requests/", list_repeat_sample_requests, name="list-repeat-sample-requests"),
    path("repeat-sample-requests/<int:request_id>/approve/", approve_repeat_sample_request, name="approve-repeat-sample-request"),
    path("repeat-sample-requests/<int:request_id>/reject/", reject_repeat_sample_request, name="reject-repeat-sample-request"),
    
    # Urgent booking request (internal users) + no-slot allocation log
    path("no-slot-allocation/log/", log_no_slot_allocation, name="log-no-slot-allocation"),
    path("booking-attempt-logs/", list_booking_attempt_logs, name="list-booking-attempt-logs"),
    path("booking-attempt-logs/my-unsuccessful/", get_my_unsuccessful_booking_attempts, name="my-unsuccessful-booking-attempts"),
    path("waitlist/my/", list_my_waitlist_entries, name="my-waitlist-entries"),
    path("waitlist/<int:entry_id>/cancel/", cancel_my_waitlist_entry, name="cancel-my-waitlist-entry"),
    path("booking-attempt-logs/<int:log_id>/quota-breakdown/", get_booking_attempt_log_quota_breakdown, name="booking-attempt-log-quota-breakdown"),
    path("booking-attempt-logs/<int:log_id>/", delete_booking_attempt_log, name="delete-booking-attempt-log"),
    path("booking-attempt-logs/log/", log_booking_attempt, name="log-booking-attempt"),
    path("urgent-booking-requests/approved-history/", get_approved_urgent_history, name="urgent-booking-requests-approved-history"),
    path("urgent-booking-requests/", list_urgent_booking_requests, name="list-urgent-booking-requests"),
    path("urgent-booking-requests/my/", list_my_urgent_booking_requests, name="list-my-urgent-booking-requests"),
    path("urgent-booking-requests/<int:request_id>/detail/", get_urgent_request_detail, name="get-urgent-request-detail"),
    path("urgent-booking-requests/wallet-pending/", list_urgent_requests_pending_wallet_approval, name="list-urgent-requests-wallet-pending"),
    path("urgent-booking-requests/wallet/", list_urgent_requests_wallet, name="list-urgent-requests-wallet"),
    path("urgent-booking-requests/create/", create_urgent_booking_request, name="create-urgent-booking-request"),
    path("urgent-booking-requests/hold-expiry-config/", urgent_hold_expiry_config, name="urgent-hold-expiry-config"),
    path("urgent-booking-requests/<int:request_id>/", update_urgent_booking_request, name="update-urgent-booking-request"),
    path("urgent-booking-requests/<int:request_id>/wallet-approve/", wallet_approve_urgent_booking_request, name="wallet-approve-urgent-booking-request"),
    path("urgent-booking-requests/<int:request_id>/evidence/", get_urgent_request_evidence, name="get-urgent-request-evidence"),
    path("users/<int:user_id>/no-slot-log/", get_no_slot_log_for_user, name="no-slot-log-for-user"),

    # Student equipment operating nominations (semester-wise, supervisor nominates)
    path("semesters/", list_semesters, name="semesters-list"),
    path("equipment-nominations/", create_equipment_nomination, name="equipment-nomination-create"),
    path("equipment-nominations/my-as-supervisor/", list_my_nominations_as_supervisor, name="equipment-nominations-my-supervisor"),
    path("equipment-nominations/my-as-student/", list_my_nominations_as_student, name="equipment-nominations-my-student"),
    path("equipment-nominations/<int:nomination_id>/submit-resume/", submit_nomination_resume, name="equipment-nomination-submit-resume"),
    path("equipment-nominations/<int:nomination_id>/resume/", get_nomination_resume, name="equipment-nomination-resume"),
    path("equipment-nominations/<int:nomination_id>/revoke/", revoke_equipment_nomination, name="equipment-nomination-revoke"),
    path("equipment-nominations/admin/", list_equipment_nominations_admin, name="equipment-nominations-admin-list"),
    path("equipment-nominations/<int:nomination_id>/approve/", approve_equipment_nomination, name="equipment-nomination-approve"),
    path("equipment-nominations/<int:nomination_id>/reject/", reject_equipment_nomination, name="equipment-nomination-reject"),

    # TA nomination call (OIC/Admin initiates; email to all Faculty)
    path("ta-nomination-calls/", create_ta_nomination_call, name="ta-nomination-call-create"),
    path("ta-nomination-calls/list/", list_ta_nomination_calls, name="ta-nomination-calls-list"),
    path("ta-nomination-calls/open-for-faculty/", list_open_ta_calls_faculty, name="ta-nomination-calls-open-faculty"),

    # TA rewards and duty logs
    path("rewards/me/summary/", my_reward_summary, name="rewards-my-summary"),
    path("rewards/me/ledger/", my_reward_ledger, name="rewards-my-ledger"),
    path("ta-assignments/allocate/", allocate_ta_assignment, name="ta-assignment-allocate"),
    path("ta-assignments/list/", list_ta_assignments, name="ta-assignment-list"),
    path("ta-assignments/<int:assignment_id>/respond/", respond_ta_assignment, name="ta-assignment-respond"),
    path("ta-assignments/<int:assignment_id>/cancel/", cancel_ta_assignment, name="ta-assignment-cancel"),
    path("ta-duty-logs/", create_ta_duty_log, name="ta-duty-log-create"),
    path("ta-duty-logs/list/", list_ta_duty_logs, name="ta-duty-log-list"),
    path("ta-duty-logs/<int:duty_log_id>/verify/", verify_ta_duty_log, name="ta-duty-log-verify"),
    path("ta-duty-logs/<int:duty_log_id>/reject/", reject_ta_duty_log, name="ta-duty-log-reject"),
    path("admin/rewards/config/", reward_config_view, name="admin-rewards-config"),
    path("oic/equipment-accessories/", oic_equipment_accessories_list, name="oic-equipment-accessories"),
    path(
        "oic/equipment-accessories/<int:accessory_id>/",
        oic_toggle_equipment_accessory,
        name="oic-toggle-equipment-accessory",
    ),
    path(
        "oic/equipment-additional-accessories/<int:accessory_id>/",
        oic_toggle_equipment_additional_accessory,
        name="oic-toggle-equipment-additional-accessory",
    ),
    path("oic/print-materials/", oic_print_materials, name="oic-print-materials"),
    path(
        "oic/print-materials/<int:material_id>/",
        oic_print_material_detail,
        name="oic-print-material-detail",
    ),
    path("oic/equipment-group-quotas/", oic_equipment_group_quotas, name="oic-equipment-group-quotas"),
    path(
        "oic/equipment-group-quotas/<int:group_id>/",
        oic_equipment_group_quotas,
        name="oic-equipment-group-quotas-detail",
    ),
    path("oic/multi-mode/", oic_multi_mode_list, name="oic-multi-mode-list"),
    path("oic/multi-mode/schedules/", oic_multi_mode_schedule_create, name="oic-multi-mode-schedule-create"),
    path(
        "oic/multi-mode/schedules/<int:schedule_id>/",
        oic_multi_mode_schedule_detail,
        name="oic-multi-mode-schedule-detail",
    ),
    path("admin/rewards/adjust/", admin_adjust_reward_points, name="admin-rewards-adjust"),
    path("inventory/items/", inventory_items_list, name="inventory-items-list"),
    path("inventory/equipments/<int:equipment_id>/stock/", equipment_inventory_stock, name="equipment-inventory-stock"),
    path("inventory/requests/", inventory_requests, name="inventory-requests"),
    path("inventory/requests/<int:request_id>/decide/", inventory_request_decide, name="inventory-request-decide"),
    path("inventory/requests/<int:request_id>/issue/", inventory_request_issue, name="inventory-request-issue"),
    path("inventory/stock/add/", inventory_stock_add, name="inventory-stock-add"),
    path("procurement/requests/", procurement_requests, name="procurement-requests"),
    path("procurement/requests/<int:request_id>/oic-endorse/", procurement_request_oic_endorse, name="procurement-request-oic-endorse"),
    path("procurement/requests/<int:request_id>/office-verify/", procurement_request_office_verify, name="procurement-request-office-verify"),
    path("procurement/requests/<int:request_id>/store-approve/", procurement_request_store_approve, name="procurement-request-store-approve"),
    path("procurement/requests/<int:request_id>/head-approve/", procurement_request_head_approve, name="procurement-request-head-approve"),
    path("procurement/requests/<int:request_id>/purchase-complete/", procurement_request_mark_purchase_complete, name="procurement-request-purchase-complete"),
    path("procurement/requests/<int:request_id>/office-seen/", procurement_request_mark_office_seen, name="procurement-request-office-seen"),
    path("equipment/lifecycle/equipment-choices/", equipment_lifecycle_equipment_choices, name="equipment-lifecycle-equipment-choices"),
    path("equipment/<int:equipment_id>/lifecycle/", equipment_lifecycle_detail, name="equipment-lifecycle-detail"),
    path("equipment/<int:equipment_id>/amc-contracts/", equipment_amc_contracts, name="equipment-amc-contracts"),
    path("equipment/<int:equipment_id>/expenses/", equipment_expenses, name="equipment-expenses"),
    path("equipment/write-off-requests/", equipment_write_off_requests, name="equipment-write-off-requests"),
    path("equipment/write-off-requests/<int:write_off_id>/office-action/", equipment_write_off_office_action, name="equipment-write-off-office"),
    path("equipment/write-off-requests/<int:write_off_id>/store-action/", equipment_write_off_store_action, name="equipment-write-off-store"),
    path("equipment/write-off-requests/<int:write_off_id>/head-action/", equipment_write_off_head_action, name="equipment-write-off-head"),
    path("equipment/write-off-requests/<int:write_off_id>/execute/", equipment_write_off_execute, name="equipment-write-off-execute"),

    # Notification endpoints
    path("notifications/", get_notifications, name="notifications-list"),
    path("notifications/<int:notification_id>/mark-read/", mark_notification_as_read, name="notification-mark-read"),
    path("notifications/mark-all-read/", mark_all_notifications_as_read, name="notifications-mark-all-read"),
    path("notifications/<int:notification_id>/", delete_notification, name="notification-delete"),
    
    # Notice Board endpoints
    path("notices/", notice_list, name="notice-list"),  # GET (public) and POST (admin)
    path("notices/<int:notice_id>/", notice_detail, name="notice-detail"),
    
    # Support Ticket endpoints
    path("tickets/", ticket_list, name="ticket-list"),  # GET and POST (public can POST)
    path("tickets/assignees/", ticket_assignees_search, name="ticket-assignees-search"),
    path("tickets/<int:ticket_id>/", ticket_detail, name="ticket-detail"),
    path("tickets/<int:ticket_id>/attachment/", ticket_attachment, name="ticket-attachment"),
    path("tickets/<int:ticket_id>/comments/", ticket_comments_list, name="ticket-comments-list"),
    path("tickets/<int:ticket_id>/comments/create/", ticket_comment_create, name="ticket-comment-create"),
    path("tickets/<int:ticket_id>/events/", ticket_events_list, name="ticket-events-list"),
    path("ticket-types/", ticket_type_list, name="ticket-type-list"),
    path("chat-agent/", chat_agent, name="chat-agent"),
    path("portal-feedback/me/", portal_feedback_mine, name="portal-feedback-me"),
    path("portal-feedback/", portal_feedback_admin_list, name="portal-feedback-admin-list"),

    # CMS (public read-only for main page and menu)
    path("cms/menu/", menu_list, name="cms-menu"),
    path("cms/home/", home_page_content, name="cms-home"),
    path("cms/site-stats/", site_stats, name="cms-site-stats"),
    path("cms/hero-slides/", hero_slides, name="cms-hero-slides"),
    path("cms/pages/<slug:slug>/", page_by_slug, name="cms-page-by-slug"),

    # Admin-only CRUD API for frontend admin dashboard (no Django Admin login required)
    path("admin/equipment-form-choices/", equipment_form_choices, name="admin-equipment-form-choices"),
    path("admin/bulk-email-recipients/", bulk_email_recipients, name="admin-bulk-email-recipients"),
    path("admin/send-bulk-email/", send_bulk_email, name="admin-send-bulk-email"),
    path(
        "admin/admin-panel-access/me/",
        __import__("config.admin_panel_access_api", fromlist=["AdminPanelAccessMeView"]).AdminPanelAccessMeView.as_view(),
        name="admin-panel-access-me",
    ),
    path(
        "admin/admin-panel-access/registry/",
        __import__("config.admin_panel_access_api", fromlist=["AdminPanelAccessRegistryView"]).AdminPanelAccessRegistryView.as_view(),
        name="admin-panel-access-registry",
    ),
    path(
        "admin/admin-panel-access/upsert/",
        __import__("config.admin_panel_access_api", fromlist=["AdminPanelRoleConfigViewSet"]).AdminPanelRoleConfigViewSet.as_view({"post": "upsert"}),
        name="admin-panel-access-upsert",
    ),
    path("admin/", include(admin_api_router().urls)),
]

# Create API root view with access to all urlpatterns
api_root = create_api_root_view(urlpatterns)

# Add API root as the first route
urlpatterns = [
    path("", api_root, name="api-root"),
] + urlpatterns
