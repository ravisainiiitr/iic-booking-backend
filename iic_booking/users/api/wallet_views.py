"""API views for wallet management."""

import hashlib
import hmac
import re
from decimal import Decimal

import razorpay
from django.conf import settings
from django.core import signing
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status

from ..models import (
    Wallet,
    User,
    SubWallet,
    SubWalletTransaction,
    WalletRazorpayOrder,
    WalletJoinRequest,
    WalletJoinRequestStatus,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
    WalletRechargeImportRecord,
    WalletRechargeParseEntry,
    ExternalUserBankDetails,
    WalletWithdrawalRequest,
    WalletWithdrawalRequestStatus,
    WalletSricSettings,
)
from ..models.user_type import UserType
from ..models.department import Department, DepartmentType
from ..repositories.wallet_repository import (
    WalletRepository,
    SubWalletRepository,
    SubWalletTransactionRepository,
    get_internal_departments_with_equipment,
    get_departments_for_wallet_recharge,
    resolve_internal_department_for_wallet_recharge,
)
from ..serializers.wallet_serializer import (
    WalletSerializer,
    WalletBalanceSerializer,
    SubWalletSerializer,
    SubWalletTransactionSerializer,
    WalletJoinRequestSerializer,
    WalletJoinRequestCreateSerializer,
    WalletJoinRequestResponseSerializer,
    WalletCreditSerializer,
    WalletRechargeRequestSerializer,
    WalletRechargeRequestCreateSerializer,
    WalletRechargeRequestVerifyUserOtpSerializer,
    WalletRechargeRequestApproveSerializer,
    ExternalUserBankDetailsSerializer,
    ExternalUserBankDetailsUpsertSerializer,
    WalletWithdrawalRequestSerializer,
    WalletWithdrawalRequestCreateSerializer,
)
from iic_booking.communication.styled_transactional_emails import (
    send_wallet_join_request_submitted_emails,
    send_wallet_join_request_decision_email,
    send_wallet_recharge_approved_faculty_email,
)


def _is_wallet_recharge_ops_staff(user) -> bool:
    """Admin or Accounts In Charge (finance): wallet recharge parse, IMAP, manual credit."""
    ut = getattr(user, "user_type", None)
    return ut in (UserType.ADMIN, UserType.FINANCE)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet(request):
    """Get current user's wallet.
    
    For students: Returns the faculty wallet they're joined to (if approved).
    For other eligible users: Returns their own wallet.
    
    Returns:
        - wallet: Wallet information with balance and transactions
        - is_shared: True if student is using a faculty wallet, False otherwise
    """
    # Use get_accessible_wallet to support students accessing faculty wallets
    wallet = request.user.get_accessible_wallet()
    
    if not wallet:
        # Auto-create wallet if user is eligible for their own wallet
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
            is_shared = False
        else:
            return Response(
                {"error": "You don't have access to any wallet. Request to join a faculty wallet if you're a student or 'Other' type user."},
                status=status.HTTP_403_FORBIDDEN,
            )
    else:
        # Check if this is a shared wallet (student or 'Other' user using faculty wallet)
        is_shared = (request.user.user_type in {UserType.STUDENT, UserType.OTHER} and 
                    wallet.user.user_type == UserType.FACULTY)
    
    # Wallet balance = consolidated sum of all sub-wallets; no main-wallet transactions
    response_data = {
        "balance": str(wallet.total_balance),
        "transactions": [],
        "is_shared": is_shared,
    }
    
    # If shared wallet, include Supervisor profile information
    if is_shared and wallet.user:
        owner = wallet.user
        owner_profile_picture = owner.get_profile_picture_url_or_none()
        response_data["wallet_owner"] = {
            "id": owner.id,
            "email": owner.email,
            "name": owner.name or owner.email,
            "phone": owner.phone_number,
            "profile_picture": owner_profile_picture,
        }
    else:
        response_data["wallet_owner"] = None

    # Include sub-wallets (department-wise balances)
    sub_wallets = SubWalletRepository.get_by_wallet(wallet)
    response_data["sub_wallets"] = SubWalletSerializer(sub_wallets, many=True).data

    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wallet_bank_details(request):
    """Get or upsert bank details for external users."""
    if not request.user.user_type or not UserType.is_external_user(request.user.user_type):
        return Response({"error": "Only external users can manage bank details."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        details = ExternalUserBankDetails.objects.filter(user=request.user).first()
        if not details:
            return Response({"bank_details": None}, status=status.HTTP_200_OK)
        return Response({"bank_details": ExternalUserBankDetailsSerializer(details).data}, status=status.HTTP_200_OK)
    # POST (upsert)
    serializer = ExternalUserBankDetailsUpsertSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    details, _ = ExternalUserBankDetails.objects.update_or_create(
        user=request.user,
        defaults={
            "account_holder_name": data["account_holder_name"].strip(),
            "bank_name": data["bank_name"].strip(),
            "account_number": str(data["account_number"]).strip(),
            "ifsc_code": str(data["ifsc_code"]).strip().upper(),
            "branch_name": (data.get("branch_name") or "").strip(),
            "account_type": (data.get("account_type") or "").strip(),
            "upi_id": (data.get("upi_id") or "").strip(),
        },
    )
    return Response({"bank_details": ExternalUserBankDetailsSerializer(details).data}, status=status.HTTP_200_OK)


def _allocate_and_debit_for_withdrawal(wallet: Wallet, amount: Decimal, request_id: int, related_user=None):
    """Allocate withdrawal across sub-wallets and debit them. Returns allocations list."""
    from django.db import transaction as db_transaction

    remaining = Decimal(str(amount))
    allocations = []
    # Prefer larger balances first to minimize rows
    sub_wallets = list(SubWallet.objects.filter(wallet=wallet, balance__gt=0).select_related("department").order_by("-balance"))
    if not sub_wallets:
        raise ValueError("No sub-wallets with positive balance.")
    if wallet.total_balance < remaining:
        raise ValueError("Insufficient wallet balance.")

    with db_transaction.atomic():
        for sw in sub_wallets:
            if remaining <= 0:
                break
            sw.refresh_from_db()
            available = sw.balance
            if available <= 0:
                continue
            take = remaining if available >= remaining else available
            if take <= 0:
                continue
            sw.debit(take, description=f"Wallet withdrawal request #{request_id} (PENDING)", related_user=related_user)
            allocations.append(
                {
                    "sub_wallet_id": sw.id,
                    "department_id": sw.department_id,
                    "amount": str(take),
                }
            )
            remaining -= take
        if remaining > 0:
            raise ValueError("Insufficient wallet balance across sub-wallets.")
    return allocations


def _credit_back_withdrawal_allocations(allocations: list, request_id: int, related_user=None):
    from django.db import transaction as db_transaction

    with db_transaction.atomic():
        for a in allocations or []:
            try:
                sw_id = int(a.get("sub_wallet_id"))
                amt = Decimal(str(a.get("amount")))
            except Exception:
                continue
            if amt <= 0:
                continue
            sw = SubWallet.objects.select_related("department").get(pk=sw_id)
            sw.credit(amt, description=f"Wallet withdrawal request #{request_id} reversal", related_user=related_user)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_wallet_withdrawal_request(request):
    """Create a withdrawal request for external users.

    Debits wallet funds immediately (held in system) until request is completed/rejected/cancelled.
    """
    if not request.user.user_type or not UserType.is_external_user(request.user.user_type):
        return Response({"error": "Only external users can create withdrawal requests."}, status=status.HTTP_403_FORBIDDEN)

    # Must be user's own wallet (not shared faculty wallet)
    wallet = request.user.get_accessible_wallet()
    if not wallet or wallet.user_id != request.user.id:
        return Response({"error": "Withdrawal is only allowed from your own wallet."}, status=status.HTTP_403_FORBIDDEN)

    details = ExternalUserBankDetails.objects.filter(user=request.user).first()
    if not details:
        return Response({"error": "Bank details are required before requesting withdrawal."}, status=status.HTTP_400_BAD_REQUEST)

    serializer = WalletWithdrawalRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    amount = serializer.validated_data["amount"]
    user_note = (serializer.validated_data.get("user_note") or "").strip()
    if amount <= 0:
        return Response({"error": "Amount must be positive."}, status=status.HTTP_400_BAD_REQUEST)

    # Create request first to get ID for descriptions
    withdrawal = WalletWithdrawalRequest.objects.create(
        user=request.user,
        wallet=wallet,
        amount=amount,
        status=WalletWithdrawalRequestStatus.PENDING,
        user_note=user_note,
        bank_snapshot={
            "account_holder_name": details.account_holder_name,
            "bank_name": details.bank_name,
            "account_number_masked": details.masked_account_number(),
            "ifsc_code": details.ifsc_code,
            "branch_name": details.branch_name,
            "account_type": details.account_type,
            "upi_id": details.upi_id,
        },
        allocations=[],
    )
    try:
        allocations = _allocate_and_debit_for_withdrawal(wallet, Decimal(str(amount)), withdrawal.id, related_user=request.user)
        withdrawal.allocations = allocations
        withdrawal.save(update_fields=["allocations", "updated_at"])
    except Exception as e:
        withdrawal.delete()
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"request": WalletWithdrawalRequestSerializer(withdrawal).data}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_withdrawal_requests(request):
    """List withdrawal requests for current user."""
    qs = WalletWithdrawalRequest.objects.filter(user=request.user).order_by("-created_at")
    return Response({"requests": WalletWithdrawalRequestSerializer(qs, many=True).data, "count": qs.count()}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_wallet_withdrawal_request(request, request_id):
    """Cancel a pending withdrawal request and credit back allocations."""
    try:
        withdrawal = WalletWithdrawalRequest.objects.get(pk=request_id, user=request.user)
    except WalletWithdrawalRequest.DoesNotExist:
        return Response({"error": "Withdrawal request not found."}, status=status.HTTP_404_NOT_FOUND)
    if withdrawal.status != WalletWithdrawalRequestStatus.PENDING:
        return Response({"error": "Only pending requests can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _credit_back_withdrawal_allocations(withdrawal.allocations, withdrawal.id, related_user=withdrawal.user)
        withdrawal.status = WalletWithdrawalRequestStatus.CANCELLED
        withdrawal.save(update_fields=["status", "updated_at"])
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({"request": WalletWithdrawalRequestSerializer(withdrawal).data, "message": "Withdrawal request cancelled."}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet_balance(request):
    """Get current user's wallet balance.
    
    For students: Returns the faculty wallet balance they're joined to (if approved).
    For other eligible users: Returns their own wallet balance.
    
    Returns:
        - balance: Current wallet balance
        - user_email: User's email
        - user_name: User's name
    """
    wallet = request.user.get_accessible_wallet()
    
    if not wallet:
        # Auto-create wallet if user is eligible for their own wallet
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        else:
            return Response(
                {"error": "You don't have access to any wallet. Request to join a faculty wallet if you're a student or 'Other' type user."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    serializer = WalletBalanceSerializer({
        "balance": wallet.total_balance
    })
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_department_wallet_balance(request):
    """
    Sub-wallet balance for an equipment's internal department (same target as booking debit).
    Optional user_id (admin only) when booking on behalf of another user.
    """
    equipment_id_raw = request.query_params.get("equipment_id")
    if not equipment_id_raw:
        return Response({"error": "equipment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        equipment_id = int(equipment_id_raw)
    except (TypeError, ValueError):
        return Response({"error": "Invalid equipment_id."}, status=status.HTTP_400_BAD_REQUEST)

    from iic_booking.equipment.models import Equipment

    equipment = get_object_or_404(
        Equipment.objects.select_related("internal_department"),
        pk=equipment_id,
    )

    target_user = request.user
    user_id_raw = request.query_params.get("user_id")
    if user_id_raw not in (None, ""):
        if getattr(request.user, "user_type", None) != UserType.ADMIN:
            return Response({"error": "Only admin can query another user's balance."}, status=status.HTTP_403_FORBIDDEN)
        try:
            target_user = User.objects.get(pk=int(user_id_raw))
        except (TypeError, ValueError, User.DoesNotExist):
            return Response({"error": "Invalid user_id."}, status=status.HTTP_400_BAD_REQUEST)

    booking_target, _is_sub = WalletRepository.get_booking_wallet_target(
        target_user,
        equipment.internal_department,
    )
    if not booking_target:
        dept = equipment.internal_department
        return Response(
            {
                "balance": "0.00",
                "has_wallet": False,
                "department_id": dept.id if dept else None,
                "department_name": (dept.name if dept else "General"),
                "department_code": (dept.code if dept else "GENERAL"),
                "is_zero": True,
            },
            status=status.HTTP_200_OK,
        )

    dept = getattr(booking_target, "department", None) or equipment.internal_department
    bal = Decimal(str(booking_target.balance)).quantize(Decimal("0.01"))
    return Response(
        {
            "balance": str(bal),
            "has_wallet": True,
            "department_id": dept.id if dept else None,
            "department_name": (dept.name if dept else "General"),
            "department_code": (dept.code if dept else "GENERAL"),
            "is_zero": bal <= 0,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet_transactions(request):
    """Get current user's wallet transaction history.
    
    For students: Returns transactions from the faculty wallet they're joined to.
    For other eligible users: Returns their own wallet transactions.
    
    Query Parameters:
        - limit: Maximum number of transactions to return (default: 50)
        - offset: Number of transactions to skip (default: 0)
    
    Returns:
        - transactions: List of wallet transactions
        - count: Total number of transactions
    """
    wallet = request.user.get_accessible_wallet()
    
    if not wallet:
        # Auto-create wallet if user is eligible for their own wallet
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        else:
            return Response(
                {"error": "You don't have access to any wallet. Request to join a faculty wallet if you're a student or 'Other' type user."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Main wallet transactions removed; use sub-wallet transactions per department
    limit = int(request.GET.get("limit", 50))
    offset = int(request.GET.get("offset", 0))
    return Response({
        "transactions": [],
        "count": 0,
        "limit": limit,
        "offset": offset,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def transfer_between_sub_wallets(request):
    """Transfer amount from one department sub-wallet to another.
    Body: { "from_department_id": int, "to_department_id": int, "amount": decimal }
    """
    wallet = request.user.get_accessible_wallet()
    if not wallet:
        return Response(
            {"error": "You don't have access to any wallet."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from_id = request.data.get("from_department_id")
    to_id = request.data.get("to_department_id")
    amount = request.data.get("amount")
    if from_id is None or to_id is None:
        return Response(
            {"error": "from_department_id and to_department_id are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if from_id == to_id:
        return Response(
            {"error": "Source and target department must be different."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if amount is None or Decimal(str(amount)) <= 0:
        return Response(
            {"error": "amount must be a positive number."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    allowed_depts = get_internal_departments_with_equipment()
    try:
        from_sw = SubWallet.objects.get(wallet=wallet, department_id=from_id)
        to_dept = allowed_depts.get(pk=to_id)
    except SubWallet.DoesNotExist:
        return Response(
            {"error": "No sub-wallet found for source department."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except Department.DoesNotExist:
        return Response(
            {"error": "Invalid target department. Choose a department that has equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    amount_decimal = Decimal(str(amount))
    try:
        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            from_sw.debit(amount_decimal, description=f"Transfer to {to_dept.name}")
            to_sw = SubWalletRepository.get_or_create(wallet, to_dept)
            to_sw.credit(amount_decimal, description=f"Transfer from {from_sw.department.name}")
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    from_sw.refresh_from_db()
    to_sw.refresh_from_db()
    return Response({
        "message": f"Transferred ₹{amount_decimal} from {from_sw.department.name} to {to_dept.name}.",
        "wallet_balance": str(wallet.total_balance),
        "from_sub_wallet": SubWalletSerializer(from_sw).data,
        "to_sub_wallet": SubWalletSerializer(to_sw).data,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_departments_for_recharge(request):
    """List internal departments valid for sub-wallet recharge (equipment-linked + existing sub-wallets)."""
    wallet = request.user.get_accessible_wallet()
    if not wallet and request.user.can_have_wallet():
        wallet, _ = WalletRepository.get_or_create(request.user)
    departments = get_departments_for_wallet_recharge(wallet)
    from ..serializers import DepartmentListSerializer
    return Response({
        "departments": DepartmentListSerializer(departments, many=True).data,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_sub_wallet_transactions(request, department_id):
    """Get transaction history for the sub-wallet of the given department.
    Query params: limit (default 50), offset (default 0).
    Each transaction includes balance_after (sub-wallet balance after that transaction).
    Students on a shared (faculty) wallet see debits related to themselves plus all credits
    (recharges are credited with related_user=wallet owner, so they must not be filtered out);
    faculty wallet owners see all transactions.
    """
    from decimal import Decimal
    from ..models import SubWalletTransaction

    wallet = request.user.get_accessible_wallet()
    if not wallet:
        return Response(
            {"error": "You don't have access to any wallet."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        sub_wallet = SubWallet.objects.get(wallet=wallet, department_id=department_id)
    except SubWallet.DoesNotExist:
        return Response(
            {"error": "No sub-wallet found for this department."},
            status=status.HTTP_404_NOT_FOUND,
        )
    limit = int(request.GET.get("limit", 50))
    offset = int(request.GET.get("offset", 0))

    # Faculty wallet owner sees all; student on shared wallet sees their debits + all credits (incl. recharges)
    is_shared_wallet_student = wallet.user_id != request.user.id
    base_qs = SubWalletTransaction.objects.filter(sub_wallet=sub_wallet).select_related(
        "sub_wallet", "sub_wallet__department", "related_user"
    ).order_by("-created_at")
    if is_shared_wallet_student:
        txn_qs = base_qs.filter(
            Q(related_user_id=request.user.id)
            | Q(transaction_type=SubWalletTransaction.TransactionType.CREDIT)
        )
    else:
        txn_qs = base_qs
    total_count = txn_qs.count()
    transactions = list(txn_qs[offset:offset + limit])

    # Compute balance_after for each transaction (newest first: current balance is after newest).
    # Use full sub-wallet history so balance_after is correct even when a student only sees their rows.
    balance_after_map = {}
    running = sub_wallet.balance
    for txn in base_qs.all():
        balance_after_map[txn.id] = running
        if txn.transaction_type == SubWalletTransaction.TransactionType.CREDIT:
            running -= txn.amount
        else:
            running += txn.amount

    serializer = SubWalletTransactionSerializer(
        transactions,
        many=True,
        context={"balance_after_map": balance_after_map},
    )
    return Response({
        "sub_wallet": SubWalletSerializer(sub_wallet).data,
        "transactions": serializer.data,
        "count": total_count,
        "limit": limit,
        "offset": offset,
    }, status=status.HTTP_200_OK)


# ============================================================================
# Wallet Join Request API Views
# ============================================================================

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_faculty_by_name(request):
    """Search faculty members by name with autocomplete support.
    
    Query Parameters:
        - q: Search query (name) (required)
        - limit: Maximum number of results (default: 10, max: 50)
    
    Returns:
        - results: List of faculty members matching the search query
    """
    query = request.query_params.get('q', '').strip()
    limit = min(int(request.query_params.get('limit', 10)), 50)
    
    if not query:
        return Response({
            "results": []
        }, status=status.HTTP_200_OK)
    
    # Search by name (case-insensitive partial match)
    faculty_queryset = User.objects.filter(
        user_type=UserType.FACULTY,
        is_active=True
    ).filter(
        Q(name__icontains=query) | Q(email__icontains=query)
    ).select_related('department')[:limit]
    
    results = []
    for faculty in faculty_queryset:
        # Check if faculty has a wallet
        has_wallet = hasattr(faculty, 'wallet')
        
        # Get profile picture URL (only if file exists in storage)
        profile_picture = faculty.get_profile_picture_url_or_none()
        
        results.append({
            "id": faculty.id,
            "name": faculty.name or faculty.email,
            "email": faculty.email,
            "phone": faculty.phone_number,
            "profile_picture": profile_picture,
            "has_wallet": has_wallet,
            "department": faculty.department.name if faculty.department else None,
            "emp_id": faculty.emp_id,
        })
    
    return Response({
        "results": results
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_faculty_by_email(request):
    """Get faculty profile by email address.
    
    Query Parameters:
        - email: Email address of the faculty member (required)
    
    Returns:
        - faculty: Faculty profile information (name, email, phone, profile_picture)
        - has_wallet: Whether the faculty has a wallet
    """
    email = request.query_params.get('email')
    if not email:
        return Response(
            {"error": "Email parameter is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        faculty = User.objects.get(email=email, user_type=UserType.FACULTY)
    except User.DoesNotExist:
        return Response(
            {"error": "Faculty member not found with the provided email address."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if faculty has a wallet
    has_wallet = hasattr(faculty, 'wallet')
    
    # Get profile picture URL (only if file exists in storage)
    profile_picture = faculty.get_profile_picture_url_or_none()
    
    return Response({
        "faculty": {
            "id": faculty.id,
            "name": faculty.name or faculty.email,
            "email": faculty.email,
            "phone": faculty.phone_number,
            "profile_picture": profile_picture,
        },
        "has_wallet": has_wallet,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_wallet_join(request):
    """Student or 'Other' user requests to join a faculty's wallet.
    
    Request Body:
        - faculty_email: Email address of the faculty member (required)
        - message: Optional message to the faculty member
    
    Returns:
        - request: Created wallet join request
    """
    # Only students and 'Other' users can request to join wallets
    if request.user.user_type not in {UserType.STUDENT, UserType.OTHER}:
        return Response(
            {"error": "Only students and 'Other' type users can request to join a faculty wallet."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    serializer = WalletJoinRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    faculty_email = serializer.validated_data['faculty_email']
    message = serializer.validated_data.get('message', '')
    
    try:
        faculty = User.objects.get(email=faculty_email, user_type=UserType.FACULTY)
    except User.DoesNotExist:
        return Response(
            {"error": "Faculty member not found with the provided email address."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if faculty has a wallet
    if not hasattr(faculty, 'wallet'):
        return Response(
            {"error": "The faculty member does not have a wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Check if there's already a pending request
    existing_request = WalletJoinRequest.objects.filter(
        student=request.user,
        faculty=faculty,
        status=WalletJoinRequestStatus.PENDING
    ).first()
    
    if existing_request:
        return Response(
            {"error": "You already have a pending request to join this faculty's wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Check if already approved
    approved_request = WalletJoinRequest.objects.filter(
        student=request.user,
        faculty=faculty,
        status=WalletJoinRequestStatus.APPROVED
    ).first()
    
    if approved_request:
        return Response(
            {"error": "You already have approved access to this faculty's wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Create the request
    try:
        join_request = WalletJoinRequest.objects.create(
            student=request.user,
            faculty=faculty,
            wallet=faculty.wallet,
            message=message,
            status=WalletJoinRequestStatus.PENDING
        )
        try:
            send_wallet_join_request_submitted_emails(join_request)
        except Exception:
            # Do not block request creation if email delivery fails.
            pass
        
        request_serializer = WalletJoinRequestSerializer(join_request)
        return Response({
            "request": request_serializer.data,
            "message": "Wallet join request sent successfully.",
        }, status=status.HTTP_201_CREATED)
    except Exception as e:
        return Response(
            {"error": f"Failed to create wallet join request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_wallet_requests(request):
    """Get wallet join requests for the current user.
    
    For students and 'Other' users: Returns requests they've sent.
    For faculty: Returns requests they've received.
    
    Query Parameters:
        - status: Filter by status (PENDING, APPROVED, REJECTED, CANCELLED)
    
    Returns:
        - requests: List of wallet join requests
    """
    if request.user.user_type in {UserType.STUDENT, UserType.OTHER}:
        # Student or 'Other' user viewing their sent requests
        queryset = WalletJoinRequest.objects.filter(student=request.user)
    elif request.user.user_type == UserType.FACULTY:
        # Faculty viewing received requests (include student + department for program details)
        queryset = WalletJoinRequest.objects.filter(faculty=request.user).select_related(
            'student', 'student__department'
        )
    else:
        return Response(
            {"error": "Only students, 'Other' type users, and faculty can view wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Filter by status if provided
    status_filter = request.query_params.get('status')
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    
    serializer = WalletJoinRequestSerializer(queryset, many=True)
    return Response({
        "requests": serializer.data,
        "count": len(serializer.data),
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_wallet_join_request(request, request_id):
    """Faculty approves a wallet join request.
    
    Request Body:
        - response_message: Optional response message to the student
    
    Returns:
        - request: Updated wallet join request
    """
    # Only faculty can approve requests
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can approve wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            faculty=request.user,
            status=WalletJoinRequestStatus.PENDING
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {"error": "Wallet join request not found or already processed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    serializer = WalletJoinRequestResponseSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    response_message = serializer.validated_data.get('response_message', '')
    
    try:
        join_request.approve(response_message)
        try:
            send_wallet_join_request_decision_email(join_request, "approved")
        except Exception:
            pass
        request_serializer = WalletJoinRequestSerializer(join_request)
        return Response({
            "request": request_serializer.data,
            "message": "Wallet join request approved successfully.",
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {"error": f"Failed to approve request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def wallet_join_email_action(request, request_id, action):
    """
    One-click signed approve/reject action from faculty email.

    URL format:
      /api/wallet/join-requests/<id>/email-action/<approve|reject>/?token=<signed-token>
    """
    action = (action or "").strip().lower()
    if action not in {"approve", "reject"}:
        return Response({"error": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)

    token = (request.query_params.get("token") or "").strip()
    if not token:
        return Response({"error": "Missing token."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data = signing.loads(token, salt="wallet-join-email-action", max_age=7 * 24 * 60 * 60)
    except signing.SignatureExpired:
        return HttpResponse(
            "<h3>Link expired</h3><p>This wallet action link has expired. Please use the Wallet page.</p>",
            status=410,
            content_type="text/html",
        )
    except signing.BadSignature:
        return Response({"error": "Invalid token."}, status=status.HTTP_400_BAD_REQUEST)

    if (
        int(data.get("request_id", -1)) != int(request_id)
        or str(data.get("action", "")).lower() != action
    ):
        return Response({"error": "Token payload mismatch."}, status=status.HTTP_400_BAD_REQUEST)

    faculty_id = data.get("faculty_id")
    try:
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            faculty_id=faculty_id,
            status=WalletJoinRequestStatus.PENDING,
        )
    except WalletJoinRequest.DoesNotExist:
        return HttpResponse(
            "<h3>Already processed</h3><p>This request was already processed or does not exist.</p>",
            status=200,
            content_type="text/html",
        )

    try:
        if action == "approve":
            join_request.approve("Approved via secure email link.")
            send_wallet_join_request_decision_email(join_request, "approved")
            status_label = "approved"
        else:
            join_request.reject("Rejected via secure email link.")
            send_wallet_join_request_decision_email(join_request, "rejected")
            status_label = "rejected"
    except Exception as exc:
        return Response({"error": f"Failed to process request: {exc}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return HttpResponse(
        (
            "<!doctype html><html><body style='font-family:Arial,sans-serif;background:#f5f7fb;padding:24px;'>"
            "<div style='max-width:620px;margin:0 auto;background:#fff;border-radius:12px;padding:20px;border:1px solid #e5e7eb;'>"
            f"<h2 style='margin:0 0 8px 0;color:#111827;'>Wallet request {status_label}</h2>"
            f"<p style='margin:0;color:#374151;'>Student <b>{join_request.student.email}</b> request has been {status_label} successfully.</p>"
            "</div></body></html>"
        ),
        status=200,
        content_type="text/html",
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_wallet_join_request(request, request_id):
    """Faculty rejects a wallet join request.
    
    Request Body:
        - response_message: Optional response message to the student
    
    Returns:
        - request: Updated wallet join request
    """
    # Only faculty can reject requests
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can reject wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            faculty=request.user,
            status=WalletJoinRequestStatus.PENDING
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {"error": "Wallet join request not found or already processed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    serializer = WalletJoinRequestResponseSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    response_message = serializer.validated_data.get('response_message', '')
    
    try:
        join_request.reject(response_message)
        try:
            send_wallet_join_request_decision_email(join_request, "rejected")
        except Exception:
            pass
        request_serializer = WalletJoinRequestSerializer(join_request)
        return Response({
            "request": request_serializer.data,
            "message": "Wallet join request rejected.",
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {"error": f"Failed to reject request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_wallet_join_request(request, request_id):
    """Student or 'Other' user cancels their own wallet join request or leaves their current wallet.
    
    This allows students and 'Other' users to:
    - Cancel pending requests
    - Leave their current wallet (cancel approved request) to switch to a different faculty wallet
    
    Returns:
        - request: Updated wallet join request
    """
    # Only students and 'Other' users can cancel their own requests
    if request.user.user_type not in {UserType.STUDENT, UserType.OTHER}:
        return Response(
            {"error": "Only students and 'Other' type users can cancel their wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        # Allow canceling both PENDING and APPROVED requests
        # APPROVED requests can be cancelled to allow students to switch wallets
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            student=request.user,
            status__in=[WalletJoinRequestStatus.PENDING, WalletJoinRequestStatus.APPROVED]
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {"error": "Wallet join request not found or cannot be cancelled."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        join_request.cancel()
        try:
            send_wallet_join_request_decision_email(join_request, "cancelled")
        except Exception:
            pass
        message = "Wallet join request cancelled." if join_request.status == WalletJoinRequestStatus.PENDING else "You have left the wallet. You can now request to join a different faculty wallet."
        request_serializer = WalletJoinRequestSerializer(join_request)
        return Response({
            "request": request_serializer.data,
            "message": message,
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {"error": f"Failed to cancel request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_student_from_wallet(request, request_id):
    """Faculty removes a student from their wallet.
    
    This allows faculty to revoke a student's access to their wallet.
    The student must have an APPROVED request.
    
    Request Body:
        - response_message: Optional message to the student explaining the removal
    
    Returns:
        - request: Updated wallet join request
    """
    # Only faculty can remove students from their wallets
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can remove students from their wallets."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        # Only APPROVED requests can be removed
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            faculty=request.user,
            status=WalletJoinRequestStatus.APPROVED
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {"error": "Approved wallet join request not found. Only approved requests can be removed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    serializer = WalletJoinRequestResponseSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    response_message = serializer.validated_data.get('response_message', '')
    
    try:
        join_request.remove(response_message)
        try:
            send_wallet_join_request_decision_email(join_request, "removed")
        except Exception:
            pass
        request_serializer = WalletJoinRequestSerializer(join_request)
        return Response({
            "request": request_serializer.data,
            "message": f"Student {join_request.student.email} has been removed from your wallet.",
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {"error": f"Failed to remove student: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delete_wallet_join_request(request, request_id):
    """Faculty permanently deletes a cancelled join request from their list.

    The student may send a new request afterward. Only CANCELLED requests can be deleted.
    """
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can delete wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        join_request = WalletJoinRequest.objects.get(
            pk=request_id,
            faculty=request.user,
            status=WalletJoinRequestStatus.CANCELLED,
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {
                "error": "Cancelled wallet join request not found, or this request cannot be deleted.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        join_request.delete()
        return Response(
            {"message": "Cancelled join request removed from your list."},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to delete request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def bulk_delete_wallet_join_requests(request):
    """Faculty permanently deletes multiple cancelled join requests at once.

    Request body: ``request_ids`` — non-empty list of join request primary keys.
    Only requests that belong to this faculty and are ``CANCELLED`` are deleted.
    """
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can delete wallet join requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    raw_ids = request.data.get("request_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) == 0:
        return Response(
            {"error": "request_ids must be a non-empty list."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        ids = list({int(x) for x in raw_ids})
    except (TypeError, ValueError):
        return Response(
            {"error": "request_ids must contain integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    qs = WalletJoinRequest.objects.filter(
        pk__in=ids,
        faculty=request.user,
        status=WalletJoinRequestStatus.CANCELLED,
    )
    deleted_count = qs.count()

    try:
        qs.delete()
        return Response(
            {
                "message": (
                    f"Removed {deleted_count} cancelled join request"
                    f"{'s' if deleted_count != 1 else ''} from your list."
                ),
                "deleted_count": deleted_count,
                "requested_count": len(ids),
            },
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to delete requests: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_wallet_join_request_notification(request, request_id):
    """
    Resend notification email for an existing wallet join request.

    Allowed for the request owner (student/'Other') when request is not CANCELLED.
    """
    if request.user.user_type not in {UserType.STUDENT, UserType.OTHER}:
        return Response(
            {"error": "Only students and 'Other' type users can resend wallet join request notifications."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        join_request = WalletJoinRequest.objects.select_related("student", "faculty").get(
            pk=request_id,
            student=request.user,
        )
    except WalletJoinRequest.DoesNotExist:
        return Response(
            {"error": "Wallet join request not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if join_request.status == WalletJoinRequestStatus.CANCELLED:
        return Response(
            {"error": "Resend is not allowed for cancelled requests."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        send_wallet_join_request_submitted_emails(join_request)
        return Response(
            {"message": "Wallet join request notification resent successfully."},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to resend request notification: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ============================================================================
# Razorpay Payment API Views
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_razorpay_order(request):
    """Create a Razorpay order for sub-wallet recharge.
    Request Body: amount (required), department_id (required, internal department).
    """
    serializer = WalletCreditSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    amount = serializer.validated_data['amount']
    department_id = request.data.get('department_id')
    if not department_id:
        return Response(
            {"error": "department_id is required. Recharge credits a department sub-wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    wallet = request.user.get_accessible_wallet()
    if not wallet:
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        if not wallet:
            return Response(
                {"error": "You don't have access to any wallet."},
                status=status.HTTP_403_FORBIDDEN,
            )
    department = resolve_internal_department_for_wallet_recharge(wallet, int(department_id))
    if not department:
        return Response(
            {"error": "Invalid department. Choose a department from the recharge list or one that already has a sub-wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    razorpay_key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
    razorpay_key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    if not razorpay_key_id or not razorpay_key_secret:
        return Response(
            {"error": "Razorpay credentials are not configured. Please contact administrator."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    client = razorpay.Client(auth=(razorpay_key_id, razorpay_key_secret))
    amount_in_paise = int(float(amount) * 100)
    try:
        order_data = {
            'amount': amount_in_paise,
            'currency': 'INR',
            'receipt': f'subwallet_recharge_{request.user.id}_{wallet.id}_{department.id}',
            'notes': {
                'user_id': str(request.user.id),
                'wallet_id': str(wallet.id),
                'department_id': str(department.id),
            }
        }
        razorpay_order = client.order.create(data=order_data)
        WalletRazorpayOrder.objects.create(
            wallet=wallet,
            department=department,
            amount_paise=amount_in_paise,
            order_id=razorpay_order['id'],
        )
        return Response({
            'order_id': razorpay_order['id'],
            'amount': razorpay_order['amount'],
            'currency': razorpay_order['currency'],
            'key': razorpay_key_id,
            'wallet_id': wallet.id,
            'department_id': department.id,
        }, status=status.HTTP_201_CREATED)
    except Exception as e:
        return Response(
            {"error": f"Failed to create payment order: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_razorpay_payment(request):
    """Verify Razorpay payment and credit wallet.
    
    Request Body:
        - razorpay_order_id: Razorpay order ID
        - razorpay_payment_id: Razorpay payment ID
        - razorpay_signature: Razorpay payment signature
        - amount: Amount that was paid (for verification)
    
    Returns:
        - wallet: Updated wallet information
        - transaction: Created transaction
        - message: Success message
    """
    razorpay_order_id = request.data.get('razorpay_order_id')
    razorpay_payment_id = request.data.get('razorpay_payment_id')
    razorpay_signature = request.data.get('razorpay_signature')
    amount = request.data.get('amount')
    
    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, amount]):
        return Response(
            {"error": "Missing required payment verification parameters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get user's accessible wallet
    wallet = request.user.get_accessible_wallet()
    if not wallet:
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        else:
            return Response(
                {"error": "You don't have access to any wallet."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Verify payment signature
    razorpay_key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    if not razorpay_key_secret:
        return Response(
            {"error": "Razorpay credentials are not configured."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    
    # Verify signature
    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    generated_signature = hmac.new(
        razorpay_key_secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    if generated_signature != razorpay_signature:
        return Response(
            {"error": "Invalid payment signature. Payment verification failed."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Convert amount from paise to rupees
    try:
        amount_in_rupees = Decimal(str(float(amount) / 100))
    except (ValueError, TypeError):
        return Response(
            {"error": "Invalid amount format."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Look up which sub-wallet to credit
    try:
        razorpay_order_record = WalletRazorpayOrder.objects.get(order_id=razorpay_order_id)
    except WalletRazorpayOrder.DoesNotExist:
        return Response(
            {"error": "Invalid or expired order. Please create a new recharge."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if razorpay_order_record.wallet_id != wallet.id:
        return Response(
            {"error": "Order does not belong to your wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    department = razorpay_order_record.department
    sub_wallet = SubWalletRepository.get_or_create(wallet, department)
    try:
        description = f"Recharge via Razorpay - Order: {razorpay_order_id}, Payment: {razorpay_payment_id}"
        transaction = sub_wallet.credit(amount_in_rupees, description, related_user=wallet.user)
        razorpay_order_record.delete()
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to credit sub-wallet: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    sub_wallet.refresh_from_db()
    return Response({
        'wallet': {"balance": str(wallet.total_balance), "transactions": []},
        'transaction': SubWalletTransactionSerializer(transaction).data,
        'message': f'Successfully recharged {department.name} sub-wallet with ₹{amount_in_rupees:.2f}',
    }, status=status.HTTP_200_OK)


# ============================================================================
# Wallet Recharge Request API Views
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_user_otp_for_recharge(request):
    """Send OTP to user's email before creating recharge request.
    
    Request Body:
        - amount: Amount to recharge (required, minimum ₹100; no upper cap in app)
        - department_id: Department ID (required)
        - project_id: Project ID (required for faculty users)
    
    Returns:
        - request_id: Temporary request ID for OTP verification
        - message: Success message
    """
    serializer = WalletRechargeRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    amount = serializer.validated_data['amount']
    department_id = serializer.validated_data['department_id']
    project_id = serializer.validated_data.get('project_id')
    credit_facility_opted_in = bool(serializer.validated_data.get('credit_facility_opted_in'))

    wallet = request.user.get_accessible_wallet()
    if not wallet:
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        if not wallet:
            return Response(
                {"error": "You don't have access to any wallet. Request to join a faculty wallet if you're a student or 'Other' type user."},
                status=status.HTTP_403_FORBIDDEN,
            )
    department = resolve_internal_department_for_wallet_recharge(wallet, department_id)
    if not department:
        return Response(
            {"error": "Invalid department. Choose a department from the recharge list or one that already has a sub-wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Validate project - required for faculty users
    project = None
    if request.user.is_faculty():
        if not project_id:
            return Response(
                {"error": "Project is required for faculty users."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from ..models import Project
        try:
            project = Project.objects.get(id=project_id, faculty=request.user, is_active=True)
        except Project.DoesNotExist:
            return Response(
                {"error": "Invalid project. Project must be active and belong to you."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    sub_wallet = SubWalletRepository.get_or_create(wallet, department)
    from ..wallet_credit_facility import (
        suppress_credit_facility_offer,
        validate_credit_opt_in_for_send_otp,
    )

    if credit_facility_opted_in and suppress_credit_facility_offer(sub_wallet):
        credit_facility_opted_in = False

    credit_err = validate_credit_opt_in_for_send_otp(
        user=request.user,
        wallet=wallet,
        department=department,
        credit_facility_opted_in=credit_facility_opted_in,
        sub_wallet=sub_wallet,
    )
    if credit_err:
        return Response({"error": credit_err}, status=status.HTTP_400_BAD_REQUEST)
    # One active OTP draft per wallet+department: remove older unverified pendings so
    # credit-facility offer/suppress logic matches the current attempt (faculty flow).
    WalletRechargeRequest.objects.filter(
        user=request.user,
        wallet=wallet,
        department=department,
        status=WalletRechargeRequestStatus.PENDING,
        user_otp_verified=False,
    ).delete()
    try:
        recharge_request = WalletRechargeRequest.objects.create(
            user=request.user,
            wallet=wallet,
            department=department,
            amount=amount,
            project=project,
            status=WalletRechargeRequestStatus.PENDING,
            credit_facility_opted_in=credit_facility_opted_in,
        )
        
        # Generate user OTP
        otp = recharge_request.generate_user_otp()
        
        # Send OTP email to user
        from iic_booking.communication.service import CommunicationService
        from django.conf import settings
        from django.core.mail import send_mail
        
        subject = f"OTP for Wallet Recharge Request - ₹{amount}"
        
        # HTML email with OTP
        html_message = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                }}
                .container {{
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .header {{
                    background-color: #2563eb;
                    color: white;
                    padding: 20px;
                    text-align: center;
                    border-radius: 5px 5px 0 0;
                }}
                .content {{
                    background-color: #f9f9f9;
                    padding: 20px;
                    border: 1px solid #ddd;
                }}
                .otp-box {{
                    background-color: #fff3cd;
                    border: 2px solid #ffc107;
                    padding: 20px;
                    margin: 20px 0;
                    text-align: center;
                    border-radius: 5px;
                }}
                .otp-code {{
                    font-size: 36px;
                    font-weight: bold;
                    color: #856404;
                    letter-spacing: 8px;
                    font-family: 'Courier New', monospace;
                }}
                .footer {{
                    text-align: center;
                    padding: 20px;
                    color: #666;
                    font-size: 12px;
                }}
                .warning {{
                    background-color: #fff3cd;
                    border-left: 4px solid #ffc107;
                    padding: 10px;
                    margin: 15px 0;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Wallet Recharge Request OTP</h1>
                </div>
                <div class="content">
                    <p>Hello {request.user.name or request.user.email},</p>
                    
                    <p>You have requested to recharge your wallet with ₹{amount}.</p>
                    
                    <div class="otp-box">
                        <p style="margin: 0 0 10px 0;"><strong>Your OTP Code:</strong></p>
                        <div class="otp-code">{otp}</div>
                        <p style="margin: 10px 0 0 0; font-size: 12px; color: #856404;">This OTP expires in 10 minutes</p>
                    </div>
                    
                    <div class="warning">
                        <strong>⚠️ Important:</strong> Enter this OTP in the application to complete your recharge request. Do not share this OTP with anyone.
                    </div>
                    
                    {f'''
                    <div style="margin-top: 20px; padding: 15px; background-color: #e7f3ff; border-left: 4px solid #2563eb; border-radius: 4px;">
                        <h3 style="margin-top: 0; color: #2563eb;">Project Details</h3>
                        <p style="margin: 5px 0;"><strong>Project Name:</strong> {project.name}</p>
                        <p style="margin: 5px 0;"><strong>Project Code:</strong> {project.project_code}</p>
                        <p style="margin: 5px 0;"><strong>Agency:</strong> {project.agency}</p>
                    </div>
                    ''' if project else ''}
                </div>
                <div class="footer">
                    <p>This is an automated email from IIC Booking System.</p>
                    <p>Please do not reply to this email.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Plain text version
        message = f"""
OTP for Wallet Recharge Request

Hello {request.user.name or request.user.email},

You have requested to recharge your wallet with ₹{amount}.

Your OTP Code: {otp}
(This OTP expires in 10 minutes)

{f'''
Project Details:
- Project Name: {project.name}
- Project Code: {project.project_code}
- Agency: {project.agency}
''' if project else ''}

⚠️ Important: Enter this OTP in the application to complete your recharge request. Do not share this OTP with anyone.

This is an automated email from IIC Booking System.
Please do not reply to this email.
        """
        
        # Send email to user
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[request.user.email],
                html_message=html_message,
                fail_silently=False,
            )
        except Exception as e:
            # Log error and delete the request
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to send OTP email to {request.user.email}: {str(e)}")
            recharge_request.delete()
            return Response(
                {"error": "Failed to send OTP email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        
        return Response({
            "request_id": recharge_request.id,
            "message": "OTP has been sent to your email. Please enter the OTP to complete your recharge request.",
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        return Response(
            {"error": f"Failed to send OTP: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def _notify_accounts_team_and_faculty_after_recharge_request_user_verified(
    http_request, recharge_request
):
    """After wallet user OTP is verified: accounts OTP, email to accounts, PENDING email/push to user, SRIC hooks.

    With ATOMIC_REQUESTS=True, DB errors in notification code must not poison the outer request transaction.
    Nested atomic() blocks use savepoints so a failed CommunicationLog (etc.) can be caught without leaving
    PostgreSQL in "current transaction is aborted" state.
    """
    from django.conf import settings
    from django.core.mail import send_mail
    from django.db import transaction
    from django.urls import reverse
    from django.utils import timezone
    import logging

    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.models import CommunicationLog, CommunicationTemplate

    logger = logging.getLogger(__name__)
    ru = recharge_request.user
    recharge_request.generate_otp()

    accounts_email = getattr(settings, "ACCOUNTS_EMAIL", "accounts@iicbooking.iitr.ac.in")
    amount = recharge_request.amount

    approve_url = http_request.build_absolute_uri(
        reverse("users:approve-recharge-request", kwargs={"request_id": recharge_request.id})
    )
    reject_url = http_request.build_absolute_uri(
        reverse("users:reject-recharge-request", kwargs={"request_id": recharge_request.id})
    )

    department_name = recharge_request.department.name if recharge_request.department else "No Department"
    department_code = (
        recharge_request.department.code
        if recharge_request.department and recharge_request.department.code
        else ""
    )

    project_name = recharge_request.project.name if recharge_request.project else ""
    project_code = recharge_request.project.project_code if recharge_request.project else ""
    project_agency = recharge_request.project.agency if recharge_request.project else ""

    template_context = {
        "user_name": ru.name or ru.email,
        "user_email": ru.email,
        "amount": f"{amount:.2f}",
        "request_id": str(recharge_request.id),
        "request_date": recharge_request.created_at.strftime("%Y-%m-%d %H:%M:%S")
        if recharge_request.created_at
        else "",
        "project_name": project_name,
        "project_code": project_code,
        "project_agency": project_agency,
        "project_details": recharge_request.project_details or "",
        "department_name": department_name,
        "department_code": department_code,
        "approve_url": approve_url,
        "reject_url": reject_url,
    }

    try:
        with transaction.atomic():
            template_obj = CommunicationService.get_template(
                template="wallet_recharge_request_email",
                communication_type=CommunicationTemplate.CommunicationType.EMAIL,
            )

            if template_obj:
                rendered = CommunicationService.render_template(
                    template_obj,
                    context=template_context,
                )
                subject = rendered.get("subject", f"Wallet Recharge Request - ₹{amount} - {ru.email}")
                message = rendered.get("message", "")
                html_message = rendered.get("html_message", "")

                try:
                    accounts_user = User.objects.filter(email=accounts_email).first()
                except Exception:
                    accounts_user = None

                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[accounts_email],
                    html_message=html_message,
                    fail_silently=False,
                )

                if accounts_user:
                    CommunicationLog.objects.create(
                        communication_type=CommunicationLog.CommunicationType.EMAIL,
                        recipient=accounts_user,
                        template=template_obj,
                        subject=subject,
                        message=message,
                        status=CommunicationLog.CommunicationStatus.SENT,
                        sent_at=timezone.now(),
                        metadata={
                            "wallet_recharge_request_id": recharge_request.id,
                            "amount": str(amount),
                            "user_email": ru.email,
                        },
                        created_by=http_request.user,
                    )
            else:
                raise ValueError("Template not found")
    except Exception as e:
        logger.warning("Failed to use template for accounts email, using fallback: %s", e)

        subject = f"Wallet Recharge Request - ₹{amount} - {ru.email}"
        html_message = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
        .content {{ background-color: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .details {{ background-color: white; padding: 15px; margin: 15px 0; border-radius: 5px; border-left: 4px solid #4CAF50; }}
        .detail-row {{ margin: 10px 0; padding: 5px 0; }}
        .detail-label {{ font-weight: bold; color: #555; }}
        .buttons {{ text-align: center; margin: 30px 0; }}
        .button {{ display: inline-block; padding: 15px 30px; margin: 10px; text-decoration: none; border-radius: 5px; font-weight: bold; font-size: 16px; color: white; }}
        .button-approve {{ background-color: #4CAF50; }}
        .button-reject {{ background-color: #f44336; }}
        .button:hover {{ opacity: 0.9; transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.2); }}
        .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
        .note {{ background-color: #fff3cd; border: 1px solid #ffc107; padding: 15px; border-radius: 5px; margin-top: 20px; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Wallet Recharge Request</h1>
        </div>
        <div class="content">
            <p>You have received a new wallet recharge request that requires your approval.</p>
            <div class="details">
                <div class="detail-row">
                    <span class="detail-label">User:</span> {ru.name or ru.email}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Email:</span> {ru.email}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Amount:</span> ₹{amount}
                </div>
                {f'<div class="detail-row"><span class="detail-label">Department:</span> {recharge_request.department.name}{f" ({recharge_request.department.code})" if recharge_request.department.code else ""}</div>' if recharge_request.department else ''}
                <div class="detail-row">
                    <span class="detail-label">Request ID:</span> #{recharge_request.id}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Request Date:</span> {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
                </div>
                {f'''
                <div class="detail-row">
                    <span class="detail-label">Project Name:</span> {recharge_request.project.name}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Project Code:</span> {recharge_request.project.project_code}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Agency:</span> {recharge_request.project.agency}
                </div>
                ''' if recharge_request.project else ''}
                {f'<div class="detail-row"><span class="detail-label">Project Details:</span> {recharge_request.project_details}</div>' if recharge_request.project_details and not recharge_request.project else ''}
            </div>
            <div class="buttons">
                <a href="{approve_url}" class="button button-approve">✓ Approve Request</a>
                <a href="{reject_url}" class="button button-reject">✗ Reject Request</a>
            </div>
            <div class="note">
                <strong>Note:</strong> Click the buttons above to approve or reject this request. Approval requires only an optional message. Rejection requires a mandatory message explaining the reason.
            </div>
        </div>
        <div class="footer">
            <p>This is an automated email from IIC Booking System.</p>
            <p>Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
        """
        department_info = ""
        if recharge_request.department:
            dept_code = f" ({recharge_request.department.code})" if recharge_request.department.code else ""
            department_info = f"- Department: {recharge_request.department.name}{dept_code}\n"

        project_info = ""
        if recharge_request.project:
            project_info = (
                f"- Project Name: {recharge_request.project.name}\n"
                f"- Project Code: {recharge_request.project.project_code}\n"
                f"- Agency: {recharge_request.project.agency}\n"
            )
        elif recharge_request.project_details:
            project_info = f"- Project Details: {recharge_request.project_details}\n"

        message = f"""
Wallet Recharge Request

You have received a new wallet recharge request that requires your approval.

Request Details:
- User: {ru.name or ru.email}
- Email: {ru.email}
- Amount: ₹{amount}
{department_info}- Request ID: #{recharge_request.id}
- Request Date: {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
{project_info}

To approve this request, click here: {approve_url}
To reject this request, click here: {reject_url}

Note: Approval requires only an optional message. Rejection requires a mandatory message explaining the reason.

This is an automated email from IIC Booking System.
Please do not reply to this email.
        """

        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[accounts_email],
            html_message=html_message,
            fail_silently=False,
        )

    try:
        with transaction.atomic():
            from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications

            send_wallet_recharge_request_notifications(recharge_request, "PENDING")
    except Exception as e:
        logger.warning("Failed to send notification to user: %s", e)

    try:
        with transaction.atomic():
            from ..wallet_recharge_ops import try_auto_sric_and_staff_alerts_after_recharge_verified

            try_auto_sric_and_staff_alerts_after_recharge_verified(http_request, recharge_request)
            recharge_request.refresh_from_db()
    except Exception as e:
        logger.warning("Post-recharge operational notifications failed: %s", e)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_wallet_recharge_request(request):
    """Create a wallet recharge request after user OTP verification.
    
    Request Body:
        - request_id: ID of the temporary request (from send_user_otp_for_recharge)
        - user_otp: 6-digit OTP code from user's email (required)
    
    Returns:
        - request: Created wallet recharge request
        - message: Success message
    """
    request_id = request.data.get('request_id')
    user_otp = request.data.get('user_otp')
    
    if not request_id or not user_otp:
        return Response(
            {"error": "request_id and user_otp are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        recharge_request = WalletRechargeRequest.objects.get(
            pk=request_id,
            user=request.user,
            status=WalletRechargeRequestStatus.PENDING,
        )
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Recharge request not found or already processed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Verify user OTP
    if not recharge_request.verify_user_otp(user_otp):
        return Response(
            {"error": "Invalid or expired OTP. Please request a new OTP."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Mark user OTP as verified
    recharge_request.mark_user_otp_verified()
    from ..wallet_credit_facility import try_activate_credit_facility_after_otp_verify

    recharge_request.refresh_from_db()
    try_activate_credit_facility_after_otp_verify(recharge_request)
    recharge_request.refresh_from_db()

    _notify_accounts_team_and_faculty_after_recharge_request_user_verified(request, recharge_request)

    request_serializer = WalletRechargeRequestSerializer(recharge_request)
    return Response({
        "request": request_serializer.data,
        "message": (
            "Wallet recharge request created successfully. "
            "The accounts team has been notified; SRIC Office and staff have been notified where configured."
        ),
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])  # Allow any since accounts team might not be users
def approve_wallet_recharge_request(request, request_id):
    """Approve a wallet recharge request (OTP no longer required).
    
    Request Body:
        - response_message: Optional response message
    
    Returns:
        - request: Updated wallet recharge request
        - transaction: Created wallet transaction
        - message: Success message
    """
    response_message = request.data.get('response_message', '').strip()
    
    try:
        recharge_request = WalletRechargeRequest.objects.get(
            pk=request_id,
            status=WalletRechargeRequestStatus.PENDING
        )
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Wallet recharge request not found or already processed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        recharge_request.approve(response_message)
        transaction_serializer = None
        department = recharge_request.department
        if not department:
            department = Department.objects.filter(
                name="General", department_type=DepartmentType.INTERNAL
            ).first()
        if department:
            sub_wallet = SubWallet.objects.filter(
                wallet=recharge_request.wallet, department=department
            ).first()
            if sub_wallet:
                txn = sub_wallet.transactions.order_by("-created_at").first()
                if txn:
                    transaction_serializer = SubWalletTransactionSerializer(txn)
        try:
            from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
            send_wallet_recharge_request_notifications(recharge_request, "APPROVED")
            try:
                send_wallet_recharge_approved_faculty_email(recharge_request)
            except Exception:
                pass
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to send approval notification: {str(e)}")
        request_serializer = WalletRechargeRequestSerializer(recharge_request)
        target = recharge_request.department.name if recharge_request.department_id else "wallet"
        return Response({
            "request": request_serializer.data,
            "transaction": transaction_serializer.data if transaction_serializer else None,
            "message": f"Wallet recharge request approved. ₹{recharge_request.amount} credited to {target}.",
        }, status=status.HTTP_200_OK)
        
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to approve request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([AllowAny])  # Allow any since accounts team might not be users
def reject_wallet_recharge_request(request, request_id):
    """Reject a wallet recharge request (OTP no longer required).
    
    Request Body:
        - response_message: Response message (required)
    
    Returns:
        - request: Updated wallet recharge request
        - message: Success message
    """
    response_message = request.data.get('response_message', '').strip()
    
    if not response_message:
        return Response(
            {"error": "Response message is required for rejection"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        recharge_request = WalletRechargeRequest.objects.get(
            pk=request_id,
            status=WalletRechargeRequestStatus.PENDING
        )
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Wallet recharge request not found or already processed."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        recharge_request.reject(response_message)
        
        # Send notification to user about rejection
        try:
            from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
            send_wallet_recharge_request_notifications(recharge_request, "REJECTED")
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to send rejection notification: {str(e)}")
        
        request_serializer = WalletRechargeRequestSerializer(recharge_request)
        
        return Response({
            "request": request_serializer.data,
            "message": "Wallet recharge request rejected.",
        }, status=status.HTTP_200_OK)
        
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to reject request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_recharge_requests(request):
    """Get wallet recharge requests for the current user.
    
    Query Parameters:
        - status: Filter by status (PENDING, APPROVED, REJECTED). Cancelled requests
          are deleted and never returned.
    
    Returns:
        - requests: List of wallet recharge requests
    """
    queryset = WalletRechargeRequest.objects.filter(user=request.user)
    
    # Filter by status if provided
    status_filter = request.query_params.get('status')
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    
    serializer = WalletRechargeRequestSerializer(queryset, many=True)
    return Response({
        "requests": serializer.data,
        "count": len(serializer.data),
    }, status=status.HTTP_200_OK)


def _parse_sric_recipient_emails(raw: str) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[\s,;]+", str(raw).strip())
    return [p.strip() for p in parts if p.strip() and "@" in p]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet_recharge_pipeline_requests(request):
    """
    Admin / Accounts In Charge: faculty-initiated recharge requests (after user OTP verified).

    Cancelled requests are never included (legacy CANCELLED rows are excluded; user cancels delete the row).

    Query: filter=all | pending | unmatched_no_parse
    - pending: still waiting for credit (PENDING).
    - unmatched_no_parse: pending and no Wallet Recharge History row yet with same Emp No. + amount.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can view the recharge pipeline."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from ..wallet_recharge_ops import (
        recharge_request_matches_parse_index,
        wallet_recharge_parse_match_index,
    )

    flt = (request.query_params.get("filter") or "all").strip().lower()
    qs = (
        WalletRechargeRequest.objects.filter(
            user__user_type=UserType.FACULTY,
            user_otp_verified=True,
        )
        .exclude(status=WalletRechargeRequestStatus.CANCELLED)
        .select_related("user", "department", "project", "wallet")
        .order_by("-created_at")[:500]
    )
    rows = list(qs)
    parse_index_for_unmatched: dict[str, set[Decimal]] | None = None
    if flt == "pending":
        rows = [r for r in rows if r.status == WalletRechargeRequestStatus.PENDING]
    elif flt == "unmatched_no_parse":
        pending = [r for r in rows if r.status == WalletRechargeRequestStatus.PENDING]
        emp_pending = {
            (r.user.emp_id or "").strip()
            for r in pending
            if r.user_id and (r.user.emp_id or "").strip()
        }
        parse_index_for_unmatched = wallet_recharge_parse_match_index(emp_pending)
        rows = [
            r
            for r in pending
            if not recharge_request_matches_parse_index(r, parse_index_for_unmatched)
        ]

    if flt == "unmatched_no_parse" and parse_index_for_unmatched is not None:
        parse_index_out = parse_index_for_unmatched
    else:
        emp_ids = {
            (r.user.emp_id or "").strip()
            for r in rows
            if r.user_id and (r.user.emp_id or "").strip()
        }
        parse_index_out = wallet_recharge_parse_match_index(emp_ids)

    out = []
    for r in rows:
        has_parse = recharge_request_matches_parse_index(r, parse_index_out)
        data = WalletRechargeRequestSerializer(r).data
        data["has_matching_parse_entry"] = has_parse
        data["user_emp_id"] = (r.user.emp_id or "").strip() if r.user_id else ""
        out.append(data)
    return Response({"requests": out, "count": len(out), "filter": flt}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_sric_wallet_recharge_notification(request, request_id):
    """Faculty: send wallet recharge request email to SRIC Office (after user OTP verified).

    Recipients and template body/subject are configured by admin (Wallet SRIC settings + Communication).
    """
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty can send SRIC Office notifications for wallet recharge."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        recharge_request = WalletRechargeRequest.objects.select_related(
            "user", "department", "project", "wallet"
        ).get(pk=request_id, user=request.user)
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Recharge request not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if recharge_request.status != WalletRechargeRequestStatus.PENDING:
        return Response(
            {"error": "Only pending requests can be sent to SRIC Office."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not recharge_request.user_otp_verified:
        return Response(
            {"error": "Verify your email OTP before sending to SRIC Office."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if recharge_request.sric_notification_sent:
        return Response(
            {"error": "SRIC Office notification has already been sent for this request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sric_settings = WalletSricSettings.get_singleton()
    recipients = _parse_sric_recipient_emails(sric_settings.recipient_emails)
    if not recipients:
        return Response(
            {
                "error": (
                    "No SRIC Office email addresses are configured. "
                    "Ask an administrator to add them under Admin → Wallet SRIC office notification settings."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    from ..wallet_recharge_ops import send_sric_faculty_recharge_email

    ok, err = send_sric_faculty_recharge_email(request, recharge_request, recipients=recipients)
    if not ok:
        return Response(
            {"error": err or "Failed to send email."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    recharge_request.sric_notification_sent = True
    recharge_request.save(update_fields=["sric_notification_sent", "updated_at"])

    request_serializer = WalletRechargeRequestSerializer(recharge_request)
    return Response(
        {
            "message": "Request sent to SRIC Office successfully.",
            "request": request_serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_wallet_recharge_request(request, request_id):
    """Remove a pending wallet recharge request (by user). The row is deleted from the database."""
    try:
        recharge_request = WalletRechargeRequest.objects.get(
            pk=request_id,
            user=request.user,
            status=WalletRechargeRequestStatus.PENDING
        )
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Wallet recharge request not found or cannot be cancelled."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        recharge_request.cancel()
        return Response(
            {
                "message": "Wallet recharge request removed.",
                "deleted_id": request_id,
            },
            status=status.HTTP_200_OK,
        )
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return Response(
            {"error": f"Failed to cancel request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_wallet_recharge_notification(request, request_id):
    """Resend notification for a pending wallet recharge request.
    
    This endpoint allows users to resend notifications (to themselves and accounts team)
    for their pending recharge requests.
    
    Returns:
        - message: Success message
    """
    try:
        recharge_request = WalletRechargeRequest.objects.get(
            pk=request_id,
            user=request.user,
            status=WalletRechargeRequestStatus.PENDING,
        )
    except WalletRechargeRequest.DoesNotExist:
        return Response(
            {"error": "Recharge request not found, already processed, or you don't have permission to access it."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        # Resend notification to user
        from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
        send_wallet_recharge_request_notifications(recharge_request, "PENDING")
        
        # Resend notification to accounts team
        from django.conf import settings
        from django.core.mail import send_mail
        from django.urls import reverse
        
        accounts_email = getattr(settings, 'ACCOUNTS_EMAIL', 'accounts@iicbooking.iitr.ac.in')
        amount = recharge_request.amount
        
        # Build approve and reject URLs (web forms, not API endpoints)
        approve_url = request.build_absolute_uri(
            reverse('users:approve-recharge-request', kwargs={'request_id': recharge_request.id})
        )
        reject_url = request.build_absolute_uri(
            reverse('users:reject-recharge-request', kwargs={'request_id': recharge_request.id})
        )
        
        department_info = ""
        if recharge_request.department:
            dept_code = f" ({recharge_request.department.code})" if recharge_request.department.code else ""
            department_info = f"- Department: {recharge_request.department.name}{dept_code}\n"
        
        project_info = ""
        if recharge_request.project:
            project_info = f"- Project Name: {recharge_request.project.name}\n- Project Code: {recharge_request.project.project_code}\n- Agency: {recharge_request.project.agency}\n"
        elif recharge_request.project_details:
            project_info = f"- Project Details: {recharge_request.project_details}\n"
        
        subject = f"Wallet Recharge Request (Resent) - ₹{amount} - {request.user.email}"
        message = f"""
Wallet Recharge Request (Resent)

You have received a wallet recharge request that requires your approval.

Request Details:
- User: {request.user.name or request.user.email}
- Email: {request.user.email}
- Amount: ₹{amount}
{department_info}- Request ID: #{recharge_request.id}
- Request Date: {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
{project_info}

To approve this request, click here: {approve_url}
To reject this request, click here: {reject_url}

Note: Approval requires only an optional message. Rejection requires a mandatory message explaining the reason.

This is an automated email from IIC Booking System.
Please do not reply to this email.
        """.strip()
        
        html_message = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
        .content {{ background-color: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .details {{ background-color: white; padding: 15px; margin: 15px 0; border-radius: 5px; border-left: 4px solid #4CAF50; }}
        .detail-row {{ margin: 10px 0; padding: 5px 0; }}
        .detail-label {{ font-weight: bold; color: #555; }}
        .buttons {{ text-align: center; margin: 30px 0; }}
        .button {{ display: inline-block; padding: 15px 30px; margin: 10px; text-decoration: none; border-radius: 5px; font-weight: bold; font-size: 16px; color: white; }}
        .button-approve {{ background-color: #4CAF50; }}
        .button-reject {{ background-color: #f44336; }}
        .button:hover {{ opacity: 0.9; transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.2); }}
        .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
        .note {{ background-color: #fff3cd; border: 1px solid #ffc107; padding: 15px; border-radius: 5px; margin-top: 20px; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Wallet Recharge Request (Resent)</h1>
        </div>
        <div class="content">
            <p>You have received a wallet recharge request that requires your approval.</p>
            <div class="details">
                <div class="detail-row">
                    <span class="detail-label">User:</span> {request.user.name or request.user.email}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Email:</span> {request.user.email}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Amount:</span> ₹{amount}
                </div>
                {f'<div class="detail-row"><span class="detail-label">Department:</span> {recharge_request.department.name}{f" ({recharge_request.department.code})" if recharge_request.department.code else ""}</div>' if recharge_request.department else ''}
                <div class="detail-row">
                    <span class="detail-label">Request ID:</span> #{recharge_request.id}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Request Date:</span> {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
                </div>
                {f'''
                <div class="detail-row">
                    <span class="detail-label">Project Name:</span> {recharge_request.project.name}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Project Code:</span> {recharge_request.project.project_code}
                </div>
                <div class="detail-row">
                    <span class="detail-label">Agency:</span> {recharge_request.project.agency}
                </div>
                ''' if recharge_request.project else ''}
                {f'<div class="detail-row"><span class="detail-label">Project Details:</span> {recharge_request.project_details}</div>' if recharge_request.project_details and not recharge_request.project else ''}
            </div>
            <div class="buttons">
                <a href="{approve_url}" class="button button-approve">✓ Approve Request</a>
                <a href="{reject_url}" class="button button-reject">✗ Reject Request</a>
            </div>
            <div class="note">
                <strong>Note:</strong> Click the buttons above to approve or reject this request. Approval requires only an optional message. Rejection requires a mandatory message explaining the reason.
            </div>
        </div>
        <div class="footer">
            <p>This is an automated email from IIC Booking System.</p>
            <p>Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
        """
        
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[accounts_email],
            html_message=html_message,
            fail_silently=False,
        )
        
        return Response({
            "message": "Notifications have been resent successfully to you and the accounts team.",
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to resend notification for recharge request {request_id}: {str(e)}")
        return Response(
            {"error": f"Failed to resend notifications: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_wallet_recharge_file(request):
    """
    Parse IIC wallet recharge text file (admin or accounts-in-charge).
    Accepts multipart/form-data with key 'file'. Returns parsed rows with matched user by emp_id.
    Each row: dated, receipt_no, amount, received_from, emp_no, matched_user { id, email, name, emp_id } or null.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can parse wallet recharge files."},
            status=status.HTTP_403_FORBIDDEN,
        )
    file_obj = request.FILES.get("file")
    if not file_obj:
        return Response(
            {"error": "No file provided. Send multipart form with key 'file'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        content = file_obj.read().decode("utf-8", errors="replace")
    except Exception as e:
        return Response(
            {"error": f"Could not read file: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_parser import parse_wallet_recharge_file as do_parse
    rows = do_parse(content)
    if not rows:
        return Response(
            {
                "rows": [],
                "message": "No rows parsed. Supported formats: (1) Pipe-delimited (e.g. IIC Wallet-27-02-2026.txt): main row starts with |digit, continuation with |\\s+|; columns 2=date, 3=receipt_no, 4=project_no, 5=amount, 6=payment, 7=received_from. (2) Tab/CSV with header: Receipt No, Amount, Received From or Name. Each data row must have a positive amount.",
            },
            status=status.HTTP_200_OK,
        )
    result = _parser_rows_to_api_result(rows)
    return Response({"rows": result, "count": len(result)}, status=status.HTTP_200_OK)


def _matched_user_dict(user: User) -> dict:
    """Serialize matched user for wallet recharge rows (name/department from DB)."""
    dept_name = ""
    if user.department_id and getattr(user, "department", None):
        dept_name = (getattr(user.department, "name", None) or "").strip()
    return {
        "id": user.id,
        "email": user.email,
        "name": (user.name or user.email or "").strip(),
        "emp_id": user.emp_id or "",
        "department_name": dept_name,
    }


def _parser_rows_to_api_result(rows):
    """Convert parser output (list of dicts) to API response rows (date, receipt_no, processed, matched_user, etc.)."""
    result = []
    for row in rows:
        dated_iso = row["dated"].isoformat() if row.get("dated") else None
        amount_val = row.get("amount")
        amount_str = f"{amount_val:,.2f}" if amount_val is not None else ""
        emp_no = row.get("emp_no") or ""
        department = row.get("dept_hint") or ""
        name = row.get("name") or ""
        payment = row.get("payment_details") or ""
        receipt_no = (row.get("receipt_no") or "").strip()
        processed = False
        if receipt_no and emp_no:
            row_dated = row.get("dated")
            qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user__emp_id=emp_no)
            if row_dated is not None:
                qs = qs.filter(dated=row_dated)
            processed = qs.exists()
        matched_user = None
        if emp_no:
            try:
                user = User.objects.select_related("department").get(emp_id=emp_no)
                matched_user = _matched_user_dict(user)
            except User.DoesNotExist:
                pass
        result.append({
            "date": dated_iso,
            "receipt_no": receipt_no,
            "name": name,
            "emp_no": emp_no,
            "department": department,
            "amount": amount_str,
            "payment": payment,
            "processed": processed,
            "matched_user": matched_user,
        })
    return result


def _parse_recharge_row_for_import(item):
    from datetime import datetime
    date_val = item.get("date")
    dated = None
    if date_val:
        if hasattr(date_val, "year"):
            dated = date_val
        else:
            s = str(date_val).strip()[:10]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    dated = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    continue
    amount_raw = str(item.get("amount") or "0").replace(",", "").strip()
    try:
        amount = Decimal(amount_raw) if amount_raw else None
    except Exception:
        amount = None
    name = (item.get("name") or "").strip()
    emp_no = (item.get("emp_no") or "").strip()
    dept = (item.get("department") or "").strip()
    received_from = name
    if emp_no:
        received_from += f" EMP NO-{emp_no}"
    if dept:
        received_from += f" DEPT-OF {dept}"
    return {
        "dated": dated,
        "receipt_no": (item.get("receipt_no") or "").strip(),
        "amount": amount,
        "emp_no": emp_no,
        "dept_hint": dept or None,
        "name": name,
        "payment_details": (item.get("payment") or "").strip(),
        "received_from": received_from.strip(),
        "remarks": "",
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def process_wallet_recharge_rows(request):
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can process wallet recharge rows."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data
    except Exception:
        data = {}
    rows_payload = data.get("rows") or []
    default_department_id = data.get("default_department_id")
    if not isinstance(rows_payload, list):
        return Response(
            {"error": "Payload must include 'rows' as a list of row objects."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    import_rows = []
    for item in rows_payload:
        if not isinstance(item, dict):
            continue
        r = _parse_recharge_row_for_import(item)
        if r["receipt_no"] and r["amount"] and r["emp_no"]:
            import_rows.append(r)
    if not import_rows:
        return Response(
            {"error": "No valid rows to process (need receipt_no, amount, emp_no)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_import import import_wallet_recharge_rows, match_pending_recharge_requests_to_parse_entries
    credited, skipped, errors, processed_receipts = import_wallet_recharge_rows(
        import_rows,
        default_department_id=default_department_id,
        dry_run=False,
    )
    matched_reqs = 0
    match_errs: list = []
    try:
        matched_reqs, match_errs = match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass
    if match_errs:
        errors = list(errors) + match_errs[:10]
    return Response({
        "credited": credited,
        "skipped": skipped,
        "errors": errors,
        "processed_receipts": processed_receipts,
        "matched_recharge_requests": matched_reqs,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_wallet_recharge_parse_entry(request):
    """
    Update one stored parse row by id (e.g. fix Emp No. when no user matched).
    If receipt/date/emp key changes, the old row is replaced. After save, if the row matches a user
    and is not yet processed, runs the same import as \"Credit matched rows\" for that row.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can apply parse entry updates."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    try:
        entry_id = int(data.get("id"))
    except (TypeError, ValueError):
        return Response({"error": "Valid entry id is required."}, status=status.HTTP_400_BAD_REQUEST)
    default_department_id = data.get("default_department_id")

    date_str = data.get("date")
    dated = None
    if date_str:
        from datetime import datetime

        s = str(date_str).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                dated = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    receipt_no = (data.get("receipt_no") or "").strip()
    emp_no = (data.get("emp_no") or "").strip()
    amount = str(data.get("amount") or "").strip()
    if not receipt_no or not emp_no or not amount:
        return Response(
            {"error": "Receipt No., Emp No., and Amount are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    entry = get_object_or_404(WalletRechargeParseEntry, pk=entry_id)
    new_name = (data.get("name") or "").strip()[:255]
    new_dept = (data.get("department") or "").strip()[:255]
    new_payment = (data.get("payment") or "")[:5000]
    preserved_source_imap_uid = entry.source_imap_uid

    old_key = (entry.receipt_no, entry.dated, entry.emp_no)
    new_key = (receipt_no, dated, emp_no)

    try:
        from django.db import transaction

        with transaction.atomic():
            if old_key != new_key:
                entry.delete()
                entry = WalletRechargeParseEntry.objects.create(
                    receipt_no=receipt_no,
                    dated=dated,
                    emp_no=emp_no,
                    name=new_name,
                    department=new_dept,
                    amount=amount[:50],
                    payment=new_payment,
                    source_imap_uid=preserved_source_imap_uid,
                )
            else:
                entry.name = new_name
                entry.department = new_dept
                entry.amount = amount[:50]
                entry.payment = new_payment
                entry.save()
    except IntegrityError:
        return Response(
            {"error": "A row with this receipt, date, and employee number already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from ..wallet_recharge_import import import_wallet_recharge_rows, match_pending_recharge_requests_to_parse_entries

    try:
        match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass

    row = _parse_entry_to_row(entry)
    credited = 0
    skipped = 0
    errors: list = []
    processed_receipts: list = []
    matched_reqs = 0

    if row.get("matched_user") and not row.get("processed"):
        item = {
            "date": row["date"],
            "receipt_no": row["receipt_no"],
            "name": row["name"],
            "emp_no": row["emp_no"],
            "department": row["department"],
            "amount": row["amount"],
            "payment": row["payment"],
        }
        r = _parse_recharge_row_for_import(item)
        if r["receipt_no"] and r["amount"] and r["emp_no"]:
            credited, skipped, errors, processed_receipts = import_wallet_recharge_rows(
                [r],
                default_department_id=default_department_id,
                dry_run=False,
            )
            try:
                matched_reqs, match_errs = match_pending_recharge_requests_to_parse_entries()
                if match_errs:
                    errors = list(errors) + match_errs[:10]
            except Exception:
                pass
            entry.refresh_from_db()
            row = _parse_entry_to_row(entry)

    return Response(
        {
            "row": row,
            "credited": credited,
            "skipped": skipped,
            "errors": errors,
            "processed_receipts": processed_receipts,
            "matched_recharge_requests": matched_reqs,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_recharge_target_user_projects(request):
    """Active projects for a faculty user (admin/finance: pick project when creating recharge request)."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list target user projects."},
            status=status.HTTP_403_FORBIDDEN,
        )
    raw_uid = request.GET.get("user_id")
    try:
        uid = int(raw_uid)
    except (TypeError, ValueError):
        return Response({"error": "user_id query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        target = User.objects.get(pk=uid)
    except User.DoesNotExist:
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    if target.user_type != UserType.FACULTY:
        return Response({"projects": []}, status=status.HTTP_200_OK)
    from ..models import Project

    out = []
    for p in Project.objects.filter(faculty=target, is_active=True).order_by("name"):
        out.append(
            {
                "id": p.id,
                "name": p.name or "",
                "project_code": (p.project_code or "").strip(),
                "agency": (p.agency or "").strip(),
            }
        )
    return Response({"projects": out}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_wallet_recharge_request_from_unmatched_parse_row(request):
    """
    Wallet Recharge History row has no directory match: ops staff selects the correct user,
    creates a pending WalletRechargeRequest (user OTP treated as verified by staff), emails
    accounts and notifies the faculty like the normal post-OTP flow.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can create a request from a parse row."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    try:
        entry_id = int(data.get("parse_entry_id") or data.get("id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "parse_entry_id (WalletRechargeParseEntry id) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "user_id (wallet user to notify and request for) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        department_id = int(data.get("department_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "department_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    project_id_raw = data.get("project_id")
    project_id = None
    if project_id_raw not in (None, ""):
        try:
            project_id = int(project_id_raw)
        except (TypeError, ValueError):
            return Response({"error": "Invalid project_id."}, status=status.HTTP_400_BAD_REQUEST)
    note = (data.get("note") or "").strip()[:500]

    entry = get_object_or_404(WalletRechargeParseEntry, pk=entry_id)
    row = _parse_entry_to_row(entry)
    if row.get("processed"):
        return Response(
            {"error": "This row is already credited via import; create a request only for unprocessed rows."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if row.get("matched_user"):
        return Response(
            {
                "error": "This row already matches a user by employee ID. Use Edit / Credit matched rows, "
                "or the standard recharge flow."
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        amount = Decimal(str((entry.amount or "").replace(",", "").strip()))
    except Exception:
        amount = Decimal("0")
    min_amt = Decimal("100")
    if amount < min_amt:
        return Response(
            {"error": f"Amount must be at least ₹{min_amt} (from parse row)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        target_user = User.objects.select_related("department").get(pk=user_id)
    except User.DoesNotExist:
        return Response({"error": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
    if not target_user.can_have_wallet():
        return Response(
            {"error": "Selected user is not eligible for a wallet recharge request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    wallet = target_user.get_accessible_wallet()
    if not wallet:
        if target_user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(target_user)
        if not wallet:
            return Response(
                {"error": "Selected user has no accessible wallet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    department = resolve_internal_department_for_wallet_recharge(wallet, department_id)
    if department is None:
        return Response(
            {"error": "Invalid department. Choose a department from the recharge list or one that already has a sub-wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from ..models import Project

    project = None
    if target_user.is_faculty():
        if not project_id:
            return Response(
                {"error": "project_id is required for faculty users."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            project = Project.objects.get(id=project_id, faculty=target_user, is_active=True)
        except Project.DoesNotExist:
            return Response(
                {"error": "Invalid project. Project must be active and belong to the selected faculty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif project_id:
        try:
            project = Project.objects.get(id=project_id, faculty=target_user, is_active=True)
        except Project.DoesNotExist:
            return Response(
                {"error": "Invalid project for this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    pd_parts = [
        "Manual request from unmatched Wallet Recharge History row.",
        f"Parse entry #{entry.id}; receipt {(entry.receipt_no or '').strip() or '—'};",
        f"file Emp No.: {(entry.emp_no or '').strip() or '—'};",
    ]
    if entry.dated:
        pd_parts.append(f"date {entry.dated.isoformat()};")
    if entry.payment:
        pd_parts.append(f"payment: {(entry.payment or '')[:400]}")
    if note:
        pd_parts.append(f"Staff note: {note}")
    project_details = " ".join(pd_parts).strip()[:4000]

    from django.db import transaction
    from ..wallet_credit_facility import try_activate_credit_facility_after_otp_verify

    with transaction.atomic():
        recharge_request = WalletRechargeRequest.objects.create(
            user=target_user,
            wallet=wallet,
            department=department,
            amount=amount,
            project=project,
            status=WalletRechargeRequestStatus.PENDING,
            user_otp_verified=True,
            project_details=project_details,
            credit_facility_opted_in=False,
        )

    recharge_request.refresh_from_db()
    try_activate_credit_facility_after_otp_verify(recharge_request)
    recharge_request.refresh_from_db()

    _notify_accounts_team_and_faculty_after_recharge_request_user_verified(request, recharge_request)

    request_serializer = WalletRechargeRequestSerializer(recharge_request)
    return Response(
        {
            "request": request_serializer.data,
            "message": (
                "Recharge request created from unmatched parse row. The faculty has been notified; "
                "the accounts team has been emailed."
            ),
        },
        status=status.HTTP_201_CREATED,
    )


def _parse_entry_to_row(entry):
    """Convert WalletRechargeParseEntry to API row with processed and matched_user."""
    row_dated = entry.dated
    emp_no = entry.emp_no or ""
    receipt_no = entry.receipt_no or ""
    qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user__emp_id=emp_no)
    if row_dated is not None:
        qs = qs.filter(dated=row_dated)
    processed = qs.exists()
    matched_user = None
    if emp_no:
        try:
            user = User.objects.select_related("department").get(emp_id=emp_no)
            matched_user = _matched_user_dict(user)
        except User.DoesNotExist:
            pass
    return {
        "id": entry.id,
        "date": entry.dated.isoformat() if entry.dated else None,
        "receipt_no": receipt_no,
        "name": entry.name or "",
        "emp_no": emp_no,
        "department": entry.department or "",
        "amount": entry.amount or "",
        "payment": entry.payment or "",
        "processed": processed,
        "matched_user": matched_user,
        "source_imap_uid": (entry.source_imap_uid or "").strip(),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet_recharge_parse_entries(request):
    """List stored parse entries (shared across devices/users). Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can access parse entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
    rows = [_parse_entry_to_row(e) for e in entries]
    return Response({"rows": rows, "count": len(rows)}, status=status.HTTP_200_OK)


def _merge_parse_entries_impl(request):
    """Merge posted rows into stored parse entries. Returns (Response or None, error_response)."""
    try:
        data = request.data
    except Exception:
        data = {}
    rows_payload = data.get("rows") or []
    if not isinstance(rows_payload, list):
        return None, Response(
            {"error": "Payload must include 'rows' as a list of row objects."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for item in rows_payload:
        if not isinstance(item, dict):
            continue
        date_str = item.get("date")
        dated = None
        if date_str:
            from datetime import datetime
            s = str(date_str).strip()[:10]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    dated = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    continue
        receipt_no = (item.get("receipt_no") or "").strip()
        emp_no = (item.get("emp_no") or "").strip()
        if not receipt_no or not emp_no:
            continue
        amount = str(item.get("amount") or "").strip()
        if not amount:
            continue
        defaults = {
            "name": (item.get("name") or "").strip()[:255],
            "department": (item.get("department") or "").strip()[:255],
            "amount": amount[:50],
            "payment": (item.get("payment") or "")[:5000],
        }
        if "source_imap_uid" in item:
            su = (item.get("source_imap_uid") or "").strip()[:32]
            defaults["source_imap_uid"] = su if su else None
        WalletRechargeParseEntry.objects.update_or_create(
            receipt_no=receipt_no,
            dated=dated,
            emp_no=emp_no,
            defaults=defaults,
        )
    try:
        from ..wallet_recharge_import import match_pending_recharge_requests_to_parse_entries

        match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass
    entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
    rows = [_parse_entry_to_row(e) for e in entries]
    return Response({"rows": rows, "count": len(rows)}, status=status.HTTP_200_OK), None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_wallet_eligible_users(request):
    """List users who may have an individual wallet (for manual recharge). Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list eligible users."},
            status=status.HTTP_403_FORBIDDEN,
        )
    search = (request.GET.get("search") or "").strip()
    qs = User.objects.filter(user_type__in=UserType.get_wallet_eligible_codes()).filter(admin_approved=True)
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(name__icontains=search) | Q(emp_id__icontains=search))
    qs = qs.order_by("name", "email")[:200]
    out = []
    for u in qs:
        phone = (u.phone_number or "").strip() or None
        phone2 = (u.secondary_phone_number or "").strip() or None
        contact_display = None
        if phone and phone2:
            contact_display = f"{phone} · {phone2}"
        elif phone:
            contact_display = phone
        elif phone2:
            contact_display = phone2
        out.append(
            {
                "id": u.id,
                "name": u.name or "",
                "email": u.email,
                "emp_id": u.emp_id or "",
                "user_type": u.user_type,
                "department_name": getattr(u.department, "name", None) if u.department_id else None,
                "department_id": u.department_id,
                "phone_number": phone,
                "secondary_phone_number": phone2,
                "contact_number": contact_display,
            }
        )
    return Response({"users": out, "count": len(out)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_manual_wallet_recharge(request):
    """
    Credit a user's sub-wallet from admin, create/update parse + import records, notify user (CC office).
    Admin or accounts-in-charge.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can perform manual wallet recharge."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    uid = data.get("user_id")
    amount_raw = data.get("amount")
    dept_id = data.get("department_id")
    receipt_no = (data.get("receipt_no") or "").strip()
    date_str = data.get("date") or data.get("dated")
    payment = (data.get("payment") or "Manual admin recharge")[:5000]
    name_override = (data.get("name") or "").strip()
    if not uid or not amount_raw or not dept_id or not receipt_no:
        return Response(
            {"error": "user_id, amount, department_id, and receipt_no are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        amount = Decimal(str(amount_raw).replace(",", "").strip())
    except Exception:
        return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
    if amount <= 0:
        return Response({"error": "Amount must be positive."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        user = User.objects.get(pk=int(uid))
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user.can_have_wallet():
        return Response({"error": "This user is not eligible for an individual wallet."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        dept = Department.objects.get(pk=int(dept_id), department_type=DepartmentType.INTERNAL)
    except (Department.DoesNotExist, TypeError, ValueError):
        return Response({"error": "Invalid internal department."}, status=status.HTTP_400_BAD_REQUEST)
    dated = None
    if date_str:
        from datetime import datetime as dt_mod

        s = str(date_str).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                dated = dt_mod.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    emp_no = (user.emp_id or "").strip()
    if not emp_no:
        return Response({"error": "User has no employee ID; cannot create parse entry key."}, status=status.HTTP_400_BAD_REQUEST)
    display_name = name_override or (user.name or "")
    received_from = display_name
    if emp_no:
        received_from = f"{received_from} EMP NO-{emp_no}".strip()
    dept_name = dept.name or ""
    if dept_name:
        received_from = f"{received_from} DEPT-OF {dept_name}".strip()
    row = {
        "receipt_no": receipt_no,
        "amount": amount,
        "emp_no": emp_no,
        "dated": dated,
        "received_from": received_from,
        "name": display_name,
        "payment_details": payment,
        "dept_hint": dept_name,
        "remarks": "Manual admin recharge",
    }
    from ..wallet_recharge_import import import_wallet_recharge_rows

    credited, skipped, errors, processed_receipts = import_wallet_recharge_rows(
        [row],
        default_department_id=dept.id,
        dry_run=False,
    )
    if credited < 1:
        return Response(
            {
                "error": errors[0] if errors else "Could not credit wallet (duplicate or validation).",
                "errors": errors,
                "skipped": skipped,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    WalletRechargeParseEntry.objects.update_or_create(
        receipt_no=receipt_no,
        dated=dated,
        emp_no=emp_no,
        defaults={
            "name": display_name[:255],
            "department": dept_name[:255],
            "amount": str(amount)[:50],
            "payment": payment,
        },
    )
    try:
        from ..wallet_recharge_import import match_pending_recharge_requests_to_parse_entries

        match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass
    entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
    rows = [_parse_entry_to_row(e) for e in entries]
    return Response(
        {
            "message": "Wallet credited and parse entry saved.",
            "processed_receipts": processed_receipts,
            "errors": errors,
            "rows": rows,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def merge_wallet_recharge_parse_entries(request):
    """Merge posted rows into stored parse entries (key: date, receipt_no, emp_no). Admin or AIC. Returns full list."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can update parse entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    resp, err = _merge_parse_entries_impl(request)
    return err if err is not None else resp


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def clear_wallet_recharge_parse_entries(request):
    """Clear all stored parse entries. Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can clear parse entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    deleted, _ = WalletRechargeParseEntry.objects.all().delete()
    return Response({"deleted": deleted, "rows": [], "count": 0}, status=status.HTTP_200_OK)


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def wallet_recharge_parse_entries(request):
    """Single endpoint: GET list, POST merge rows, DELETE clear. Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can access parse entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if request.method == "GET":
        entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
        rows = [_parse_entry_to_row(e) for e in entries]
        return Response({"rows": rows, "count": len(rows)}, status=status.HTTP_200_OK)
    if request.method == "POST":
        resp, err = _merge_parse_entries_impl(request)
        return err if err is not None else resp
    if request.method == "DELETE":
        deleted, _ = WalletRechargeParseEntry.objects.all().delete()
        return Response({"deleted": deleted, "rows": [], "count": 0}, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)


def _imap_config_from_request(data):
    """Extract IMAP config dict from request data. Returns (config_dict, error_response)."""
    try:
        d = data if isinstance(data, dict) else {}
    except Exception:
        d = {}
    email_address = (d.get("email") or "").strip()
    password = d.get("password") or ""
    host = (d.get("host") or "imap.gmail.com").strip()
    if not host:
        return None, Response({"error": "IMAP host is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not email_address:
        return None, Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not password:
        return None, Response({"error": "Password is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        port = int(d.get("port") or 993)
    except (TypeError, ValueError):
        port = 993
    use_ssl = d.get("use_ssl", True) if d.get("use_ssl") is not False else True
    folder = (d.get("folder") or "INBOX").strip() or "INBOX"
    sender_filter = (d.get("sender_filter") or "").strip() or None
    subject_filter = (d.get("subject_filter") or "").strip() or None
    return {
        "host": host,
        "port": port,
        "use_ssl": use_ssl,
        "email_address": email_address,
        "password": password,
        "folder": folder,
        "sender_filter": sender_filter,
        "subject_filter": subject_filter,
    }, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_list_emails(request):
    """
    List emails via IMAP: last 50 when no subject filter; when subject_filter is set, all matches
    (up to server cap). Admin or accounts-in-charge.
    Body: email, password, host?, port?, use_ssl?, folder?, sender_filter?, subject_filter?
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list emails."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    from ..imap_fetch import list_emails
    emails, error = list_emails(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        folder=config["folder"],
        sender_filter=config["sender_filter"],
        subject_filter=config["subject_filter"],
        max_results=50,
    )
    if error:
        return Response({"error": error, "emails": []}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"emails": emails, "count": len(emails)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_fetch_and_parse(request):
    """
    Fetch one email by UID, get first text/csv attachment, parse and return rows. Admin or AIC.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid (required).
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can fetch email attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    if not email_uid:
        return Response(
            {"error": "email_uid is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    attachment_index = data.get("attachment_index")
    if attachment_index is not None:
        try:
            attachment_index = int(attachment_index)
        except (TypeError, ValueError):
            attachment_index = None
    from ..imap_fetch import fetch_email_attachment
    content, filename, error = fetch_email_attachment(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        folder=config["folder"],
        attachment_index=attachment_index,
    )
    if error or not content:
        return Response(
            {"error": error or "No attachment content", "rows": [], "count": 0},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_parser import parse_wallet_recharge_file as do_parse
    rows = do_parse(content)
    if not rows:
        return Response(
            {
                "rows": [],
                "count": 0,
                "message": "No rows parsed from attachment. Check file format.",
                "attachment_name": filename,
            },
            status=status.HTTP_200_OK,
        )
    result = _parser_rows_to_api_result(rows)
    return Response({
        "rows": result,
        "count": len(result),
        "attachment_name": filename,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_email_attachments(request):
    """
    List attachments for one email by UID. Admin or accounts-in-charge.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid (required).
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list email attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    if not email_uid:
        return Response(
            {"error": "email_uid is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..imap_fetch import list_attachments_for_email
    attachments, error = list_attachments_for_email(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        folder=config["folder"],
    )
    if error:
        return Response({"error": error, "attachments": []}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"attachments": attachments, "count": len(attachments)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_download_attachment(request):
    """
    Download one attachment by email UID and attachment index. Admin only.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid, attachment_index (0-based).
    Returns JSON: { content_base64, filename } so frontend can trigger download.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can download attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    attachment_index = data.get("attachment_index")
    if not email_uid:
        return Response({"error": "email_uid is required."}, status=status.HTTP_400_BAD_REQUEST)
    if attachment_index is None:
        return Response({"error": "attachment_index is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        attachment_index = int(attachment_index)
    except (TypeError, ValueError):
        return Response({"error": "attachment_index must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
    import base64
    from ..imap_fetch import get_attachment_content
    content, filename, error = get_attachment_content(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        attachment_index=attachment_index,
        folder=config["folder"],
    )
    if error or content is None:
        return Response(
            {"error": error or "Could not get attachment"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    b64 = base64.b64encode(content).decode("ascii")
    return Response({"content_base64": b64, "filename": filename or "attachment"}, status=status.HTTP_200_OK)


def _all_parse_entries_processed_for_imap_uid(imap_uid: str) -> bool:
    u = (imap_uid or "").strip()
    if not u:
        return False
    qs = WalletRechargeParseEntry.objects.filter(source_imap_uid=u)
    if not qs.exists():
        return False
    for entry in qs:
        row = _parse_entry_to_row(entry)
        if not row.get("processed"):
            return False
    return True


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_delete_email_if_processed(request):
    """
    Delete one mailbox message by UID via IMAP only if every parse entry
    tagged with that source_imap_uid is processed (credited). Admin or accounts-in-charge.
    Clears source_imap_uid on those rows after successful delete.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can delete IMAP messages."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = (data.get("email_uid") or "").strip()
    if not email_uid:
        return Response({"error": "email_uid is required.", "deleted": False}, status=status.HTTP_400_BAD_REQUEST)
    if not _all_parse_entries_processed_for_imap_uid(email_uid):
        return Response(
            {
                "error": "Not all recharge rows from this email are processed yet, or no rows reference this UID.",
                "deleted": False,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..imap_fetch import delete_email_by_uid

    ok, del_err = delete_email_by_uid(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        folder=config["folder"],
        email_uid=email_uid,
    )
    if not ok:
        return Response({"error": del_err or "Delete failed", "deleted": False}, status=status.HTTP_400_BAD_REQUEST)
    WalletRechargeParseEntry.objects.filter(source_imap_uid=email_uid).update(source_imap_uid=None)
    return Response({"deleted": True, "email_uid": email_uid}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_wallet_expense_report(request):
    """
    IITR Faculty: consolidated insight for the supervisor wallet + approved linked students.
    Query params: date_from, date_to (YYYY-MM-DD), equipment_id (optional).
    """
    if getattr(request.user, "user_type", None) != UserType.FACULTY:
        return Response(
            {"error": "This report is only available for IITR Faculty."},
            status=status.HTTP_403_FORBIDDEN,
        )
    date_from = (request.query_params.get("date_from") or "").strip() or None
    date_to = (request.query_params.get("date_to") or "").strip() or None
    equipment_id_raw = (request.query_params.get("equipment_id") or "").strip()
    equipment_id: int | None = None
    if equipment_id_raw:
        try:
            equipment_id = int(equipment_id_raw)
        except ValueError:
            return Response(
                {"error": "Invalid equipment_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    from ..faculty_wallet_report import build_faculty_wallet_expense_report

    data = build_faculty_wallet_expense_report(
        request.user,
        date_from=date_from,
        date_to=date_to,
        equipment_id=equipment_id,
    )
    if data.get("error") == "only_faculty":
        return Response({"error": data.get("message", "Forbidden")}, status=status.HTTP_403_FORBIDDEN)
    return Response(data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_facility_settings_view(request):
    """Public (authenticated) read of admin-configured credit facility parameters."""
    from ..wallet_credit_facility import get_credit_settings

    cfg = get_credit_settings()
    return Response(
        {
            "balance_threshold_inr": str(cfg.balance_threshold_inr),
            "credit_window_days": int(cfg.credit_window_days),
            "max_credit_inr": str(cfg.max_credit_inr),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_facility_offer_for_recharge_view(request):
    """Faculty: whether to show the credit-facility popup before send-otp (same rules as send-otp)."""
    from ..wallet_credit_facility import credit_facility_offer_for_recharge_preflight

    raw = request.query_params.get("department_id")
    if raw is None or str(raw).strip() == "":
        return Response(
            {"error": "department_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        department_id = int(raw)
    except (TypeError, ValueError):
        return Response(
            {"error": "Invalid department_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(credit_facility_offer_for_recharge_preflight(request.user, department_id))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_facility_my_status_view(request):
    """Active / expired credit lines for the user's accessible wallet (faculty timeline / alerts)."""
    from ..wallet_credit_facility import (
        department_ids_pending_credit_opt_in,
        serialize_faculty_credit_status_for_wallet,
    )

    wallet = request.user.get_accessible_wallet()
    if not wallet:
        return Response({"items": [], "pending_credit_opt_in_department_ids": []})
    items = serialize_faculty_credit_status_for_wallet(wallet, for_user=request.user)
    pending_ids = department_ids_pending_credit_opt_in(wallet)
    return Response(
        {
            "items": items,
            "pending_credit_opt_in_department_ids": pending_ids,
        }
    )


def _is_legacy_wallet_import_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _legacy_wallet_lookup_options(request) -> tuple:
    """Parse optional batch / department from query or JSON body."""
    data = request.data if hasattr(request, "data") and request.data else {}
    if not data and hasattr(request, "query_params"):
        data = request.query_params
    department = (data.get("department") or "general").strip().lower()
    use_general = department != "user"
    department_id_raw = data.get("department_id")
    department_id = None
    if department_id_raw not in (None, ""):
        try:
            department_id = int(department_id_raw)
        except (TypeError, ValueError):
            raise ValueError("Invalid department_id.")
    batch_id = (data.get("batch_id") or "").strip() or None
    return batch_id, department_id, use_general


def _legacy_mysql_overrides_from_request(request):
    from ..legacy_wallet_db import parse_legacy_mysql_overrides

    data = request.data if hasattr(request, "data") and request.data else {}
    if not data and hasattr(request, "query_params"):
        data = request.query_params
    try:
        return parse_legacy_mysql_overrides(data.get("legacy_mysql"))
    except ValueError as e:
        raise ValueError(str(e)) from e


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def legacy_wallet_balance_lookup(request):
    """
    Admin: fetch legacy wallet balance for an emp_id directly from legacy MySQL.
    Body/query: emp_id (required), optional batch_id, department.
    Connection uses LEGACY_MYSQL_* server environment variables.
    """
    if not _is_legacy_wallet_import_admin(request.user):
        return Response(
            {"error": "Only admin users can query the legacy wallet database."},
            status=status.HTTP_403_FORBIDDEN,
        )

    data = request.data if hasattr(request, "data") and request.data else {}
    if not data and hasattr(request, "query_params"):
        data = request.query_params
    emp_id = (data.get("emp_id") or "").strip()
    if not emp_id:
        return Response({"error": "emp_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        batch_id, department_id, use_general = _legacy_wallet_lookup_options(request)
        mysql_config = _legacy_mysql_overrides_from_request(request)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    from ..legacy_wallet_import import lookup_legacy_wallet_for_emp_id

    result = lookup_legacy_wallet_for_emp_id(
        emp_id,
        batch_id=batch_id,
        department_id=department_id,
        use_general_department=use_general,
        mysql_config=mysql_config,
    )

    if result.get("status") == "not_configured":
        return Response(result, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if result.get("status") == "connection_error":
        return Response(result, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def legacy_wallet_balance_list(request):
    """
    Admin: list all legacy users with non-zero wallet balance.
    Connection uses LEGACY_MYSQL_* server environment variables.
    """
    if not _is_legacy_wallet_import_admin(request.user):
        return Response(
            {"error": "Only admin users can query the legacy wallet database."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        mysql_config = _legacy_mysql_overrides_from_request(request)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    from ..legacy_wallet_db import (
        LegacyWalletConnectionError,
        LegacyWalletNotConfigured,
        fetch_all_legacy_wallets_nonzero,
    )

    try:
        result = fetch_all_legacy_wallets_nonzero(mysql_config=mysql_config)
    except LegacyWalletNotConfigured as e:
        return Response(
            {"status": "not_configured", "error": str(e), "rows": [], "row_count": 0, "total_balance": "0.00"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except LegacyWalletConnectionError as e:
        return Response(
            {"status": "connection_error", "error": str(e), "rows": [], "row_count": 0, "total_balance": "0.00"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(result, status=status.HTTP_200_OK)

