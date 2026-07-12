"""API views for support tickets."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.decorators import parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Ticket, TicketComment, TicketTypeCode
from .serializers import TicketSerializer, TicketCreateSerializer, TicketCommentSerializer
from .chat_ai import get_ai_reply

logger = logging.getLogger(__name__)


def _status_label(status_code: str) -> str:
    return dict(Ticket.TicketStatus.choices).get(status_code, status_code)


def _priority_label(priority_code: str) -> str:
    return dict(Ticket.TicketPriority.choices).get(priority_code, priority_code)


def _send_ticket_update_email(ticket: Ticket, action_summary: str, action_details: str = "") -> None:
    """Notify the ticket owner about status/action updates."""
    recipient_email = ticket.get_user_email()
    if not recipient_email:
        return
    link = (getattr(settings, "FRONTEND_URL", "") + "/tickets") if getattr(settings, "FRONTEND_URL", None) else ""
    lines = [
        f"Hello {ticket.get_user_name()},",
        "",
        "Your support ticket has a new update.",
        "",
        f"Ticket #{ticket.ticket_id}",
        f"Subject: {ticket.subject}",
        f"Update: {action_summary}",
    ]
    if action_details:
        lines.extend(["", "Details:", action_details])
    if link:
        lines.extend(["", f"View ticket: {link}"])
    lines.extend(["", "Thank you,", "IIC Booking Team"])
    try:
        send_mail(
            subject=f"Support ticket #{ticket.ticket_id} updated",
            message="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("Failed to send ticket update email to %s: %s", recipient_email, e)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])  # Allow public access for GET and POST
@parser_classes([JSONParser, FormParser, MultiPartParser])
def ticket_list(request):
    """
    Get list of tickets (GET) or create a new ticket (POST).
    
    GET: 
        - Authenticated users: Get their own tickets
        - Staff: Get all tickets (can filter with query params)
        - Public: Not allowed (returns empty list)
    
    POST:
        - Anyone can create a ticket (public or authenticated)
        - If authenticated, user field is automatically set
        - If not authenticated, public_name, public_email, public_phone are required
    
    Query Parameters (GET):
        - status: Filter by status (open, in_progress, resolved, closed, cancelled)
        - ticket_type: Filter by type (query, request, complaint, etc.)
        - priority: Filter by priority (low, medium, high, urgent)
        - user_id: Filter by user ID (staff only)
        - assigned_to: Filter by assigned staff member (staff only)
    """
    if request.method == "GET":
        # For authenticated users, show their own tickets
        if request.user.is_authenticated:
            if request.user.is_staff:
                # Staff can see all tickets
                queryset = Ticket.objects.all()
                
                # Filter by user_id if provided (staff only)
                user_id = request.query_params.get('user_id')
                if user_id:
                    queryset = queryset.filter(user_id=user_id)
                
                # Filter by assigned_to if provided (staff only)
                assigned_to = request.query_params.get('assigned_to')
                if assigned_to:
                    queryset = queryset.filter(assigned_to_id=assigned_to)
            else:
                # Regular users see only their own tickets
                queryset = Ticket.objects.filter(user=request.user)
        else:
            # Public users cannot see tickets
            return Response(
                {"tickets": [], "count": 0},
                status=status.HTTP_200_OK,
            )
        
        # Apply filters
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        ticket_type = request.query_params.get('ticket_type')
        if ticket_type:
            # Filter by ticket type code
            queryset = queryset.filter(ticket_type=ticket_type)
        
        priority = request.query_params.get('priority')
        if priority:
            queryset = queryset.filter(priority=priority)
        
        # Order by creation date (newest first)
        queryset = queryset.order_by('-created_at')
        
        serializer = TicketSerializer(queryset, many=True)
        return Response(
            {
                "tickets": serializer.data,
                "count": len(serializer.data),
            },
            status=status.HTTP_200_OK,
        )
    
    elif request.method == "POST":
        # Anyone can create a ticket
        serializer = TicketCreateSerializer(data=request.data)
        if serializer.is_valid():
            # If user is authenticated, set user field
            if request.user.is_authenticated:
                ticket = serializer.save(user=request.user)
            else:
                # For public users, validate that public fields are provided
                if not serializer.validated_data.get('public_email'):
                    return Response(
                        {"error": "Email is required for public users."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                ticket = serializer.save()

            # Create an initial visible log entry so users can track lifecycle in portal.
            raised_by = ticket.get_user_name() if ticket.get_user_name() else "User"
            TicketComment.objects.create(
                ticket=ticket,
                user=request.user if request.user.is_authenticated else None,
                comment=f"Ticket raised by {raised_by}.",
                is_internal=False,
            )
            
            # Return full ticket data
            full_serializer = TicketSerializer(ticket)
            return Response(full_serializer.data, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "PATCH", "PUT"])
@permission_classes([AllowAny])  # Check auth manually
def ticket_detail(request, ticket_id):
    """
    Get, update a ticket.
    
    GET: 
        - Ticket creator can view their ticket
        - Staff can view any ticket
        - Public users cannot view tickets
    
    PATCH/PUT:
        - Only staff can update tickets
        - Users can only update their own tickets (limited fields)
    """
    try:
        ticket = Ticket.objects.get(ticket_id=ticket_id)
    except Ticket.DoesNotExist:
        return Response(
            {"error": "Ticket not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    if request.method == "GET":
        # Check permissions
        if not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required to view tickets."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        
        # Users can only view their own tickets, staff can view all
        if not request.user.is_staff and ticket.user != request.user:
            return Response(
                {"error": "You don't have permission to view this ticket."},
                status=status.HTTP_403_FORBIDDEN,
            )
        
        serializer = TicketSerializer(ticket)
        return Response(serializer.data)
    
    # PATCH or PUT - update ticket
    if not request.user.is_authenticated:
        return Response(
            {"error": "Authentication required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    
    # Staff can update any ticket, users can only update their own (limited)
    if not request.user.is_staff and ticket.user != request.user:
        return Response(
            {"error": "You don't have permission to update this ticket."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # For non-staff users, only allow updating description
    if not request.user.is_staff:
        if 'description' not in request.data or len(request.data) > 1:
            return Response(
                {"error": "You can only update the description of your ticket."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    old_status = ticket.status
    old_priority = ticket.priority
    old_assigned_to = ticket.assigned_to
    old_resolution_notes = ticket.resolution_notes or ""

    serializer = TicketSerializer(ticket, data=request.data, partial=(request.method == "PATCH"))
    if serializer.is_valid():
        # Handle status changes
        if 'status' in request.data:
            new_status = request.data['status']
            if new_status == Ticket.TicketStatus.RESOLVED and not ticket.resolved_at:
                ticket.resolved_at = timezone.now()
            elif new_status == Ticket.TicketStatus.CLOSED and not ticket.closed_at:
                ticket.closed_at = timezone.now()
        
        serializer.save()
        updated_ticket = serializer.instance
        if request.user.is_staff:
            action_lines = []
            if old_status != updated_ticket.status:
                action_lines.append(
                    f"Status updated from {_status_label(old_status)} to {_status_label(updated_ticket.status)}."
                )
            if old_priority != updated_ticket.priority:
                action_lines.append(
                    f"Priority changed from {_priority_label(old_priority)} to {_priority_label(updated_ticket.priority)}."
                )
            if (old_assigned_to.id if old_assigned_to else None) != (updated_ticket.assigned_to.id if updated_ticket.assigned_to else None):
                prev_assignee = (old_assigned_to.name or old_assigned_to.email) if old_assigned_to else "Unassigned"
                new_assignee = (
                    (updated_ticket.assigned_to.name or updated_ticket.assigned_to.email)
                    if updated_ticket.assigned_to
                    else "Unassigned"
                )
                action_lines.append(f"Assignment changed from {prev_assignee} to {new_assignee}.")
            if old_resolution_notes.strip() != (updated_ticket.resolution_notes or "").strip():
                action_lines.append("Resolution notes were updated.")
                if (updated_ticket.resolution_notes or "").strip():
                    action_lines.append(f"Resolution: {updated_ticket.resolution_notes.strip()}")

            if action_lines:
                TicketComment.objects.create(
                    ticket=updated_ticket,
                    user=request.user,
                    comment="\n".join(action_lines),
                    is_internal=False,
                )
                _send_ticket_update_email(
                    updated_ticket,
                    action_summary=action_lines[0],
                    action_details="\n".join(action_lines[1:]) if len(action_lines) > 1 else "",
                )

        return Response(serializer.data)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_comment_create(request, ticket_id):
    """
    Create a comment on a ticket.
    
    - Ticket creator and staff can add comments
    - Staff can mark comments as internal
    """
    try:
        ticket = Ticket.objects.get(ticket_id=ticket_id)
    except Ticket.DoesNotExist:
        return Response(
            {"error": "Ticket not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    if not request.user.is_staff and ticket.user != request.user:
        return Response(
            {"error": "You don't have permission to comment on this ticket."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    comment_text = request.data.get('comment')
    if not comment_text:
        return Response(
            {"error": "Comment text is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    is_internal = request.data.get('is_internal', False)
    # Only staff can create internal comments
    if is_internal and not request.user.is_staff:
        is_internal = False
    
    comment = TicketComment.objects.create(
        ticket=ticket,
        user=request.user,
        comment=comment_text,
        is_internal=is_internal,
    )

    # Public staff comments should be sent to the ticket owner and shown as action updates.
    if request.user.is_staff and not is_internal:
        _send_ticket_update_email(
            ticket,
            action_summary="A support team comment was added.",
            action_details=comment_text,
        )
    
    serializer = TicketCommentSerializer(comment)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ticket_comments_list(request, ticket_id):
    """
    Get all comments for a ticket.
    
    - Ticket creator sees non-internal comments
    - Staff see all comments
    """
    try:
        ticket = Ticket.objects.get(ticket_id=ticket_id)
    except Ticket.DoesNotExist:
        return Response(
            {"error": "Ticket not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    if not request.user.is_staff and ticket.user != request.user:
        return Response(
            {"error": "You don't have permission to view comments for this ticket."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Filter comments based on user role
    if request.user.is_staff:
        comments = ticket.comments.all()
    else:
        comments = ticket.comments.filter(is_internal=False)
    
    serializer = TicketCommentSerializer(comments, many=True)
    return Response(
        {
            "comments": serializer.data,
            "count": len(serializer.data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def ticket_type_list(request):
    """
    Get list of available ticket types.
    
    Public access - returns the three ticket type constants.
    """
    ticket_types = [
        {
            "code": TicketTypeCode.BOOKING,
            "name": TicketTypeCode.get_display_name(TicketTypeCode.BOOKING),
        },
        {
            "code": TicketTypeCode.EQUIPMENT,
            "name": TicketTypeCode.get_display_name(TicketTypeCode.EQUIPMENT),
        },
        {
            "code": TicketTypeCode.OTHER,
            "name": TicketTypeCode.get_display_name(TicketTypeCode.OTHER),
        },
        {
            "code": TicketTypeCode.QUALITY_IMPROVEMENT,
            "name": TicketTypeCode.get_display_name(TicketTypeCode.QUALITY_IMPROVEMENT),
        },
    ]
    return Response(
        {
            "ticket_types": ticket_types,
            "count": len(ticket_types),
        },
        status=status.HTTP_200_OK,
    )


# --- Virtual chat agent (FAQ + auto support ticket for complex queries) ---

_FAQ_RESPONSES = [
    # (keywords in user message, response)
    (["how to book", "how do i book", "book equipment", "make a booking"], (
        "To book equipment: go to Dashboard or Equipments, select the equipment you need, "
        "choose your slots and fill in the required details, then submit. You can view your bookings under My Bookings."
    )),
    (["cancel", "cancellation", "cancel booking"], (
        "You can request cancellation from My Bookings: open the booking and use the cancel option. "
        "Cancellation may be subject to policy; check the equipment page for details."
    )),
    (["slot", "slots", "availability", "available"], (
        "Slot availability is shown on each equipment's booking page. Select your date range and number of samples to see available slots. "
        "If no slots appear, try a different date or check back later."
    )),
    (["wallet", "balance", "recharge", "payment"], (
        "Wallet balance and recharge options are in the Wallet section from your dashboard. "
        "Internal users may need to use a faculty wallet or request recharge as per your institute policy."
    )),
    (["urgent", "urgent request", "no slot", "couldn't get slot"], (
        "If you couldn't get a slot after repeated attempts, you can submit an Urgent Request from the equipment page or My Urgent Requests. "
        "For 'urgent comment from reviewer' type, your supervisor must approve first; then admin/OIC will review."
    )),
    (["equipment", "list", "see equipment", "which equipment"], (
        "You can browse all available equipment from the Equipments page or the dashboard. "
        "Use filters or search to find the one you need."
    )),
    (["help", "hi", "hello", "hey"], (
        "Hello! I can help with general questions about booking equipment, slots, wallet, cancellations, and urgent requests. "
        "Type your question in a few words, or ask to speak to our team and we will create a support ticket for you."
    )),
    (["contact", "support", "ticket", "speak to", "human", "agent", "representative"], None),
]

_ESCALATION_PHRASES = [
    "speak to", "talk to", "human", "agent", "representative", "real person",
    "refund", "complaint", "complained", "escalate", "manager", "supervisor",
]


def _get_faq_reply(message):
    """Return FAQ reply if message matches a known intent, else None. None can mean 'escalate' for some intents."""
    if not message or not isinstance(message, str):
        return None
    msg = message.strip().lower()
    if len(msg) < 2:
        return None
    for keywords, response in _FAQ_RESPONSES:
        if response is None:
            if any(k in msg for k in keywords):
                return None  # escalate
        else:
            if any(k in msg for k in keywords):
                return response
    return None


def _is_complex_query(message, faq_reply):
    """Decide if we should create a support ticket (complex or escalation)."""
    if not message or not isinstance(message, str):
        return True
    msg = message.strip()
    # Explicit request for human/agent
    if any(p in msg.lower() for p in _ESCALATION_PHRASES):
        return True
    # No FAQ match and message is long or multi-sentence
    if faq_reply is None and (len(msg) > 200 or msg.count(".") + msg.count("?") >= 2):
        return True
    # No FAQ match and user wrote a lot
    if faq_reply is None and len(msg) > 80:
        return True
    return False


@api_view(["POST"])
@permission_classes([AllowAny])
@parser_classes([JSONParser])
def chat_agent(request):
    """
    AI-powered virtual chat agent for general queries.
    POST body: { "message": "user message" [, "public_name", "public_email"] }
    When OPENAI_API_KEY is set, uses OpenAI to answer; otherwise falls back to FAQ.
    If the query is complex or the AI/user asks for human support, creates a support ticket
    and returns ticket_id. Authenticated user is linked to the ticket; otherwise
    public_name/public_email can be sent, or a placeholder is used.
    """
    message = (request.data.get("message") or "").strip()
    if not message:
        return Response(
            {"error": "Message is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    public_name = (request.data.get("public_name") or "").strip() or None
    public_email = (request.data.get("public_email") or "").strip() or None

    use_ai = bool((getattr(settings, "OPENAI_API_KEY", None) or "").strip())
    if use_ai:
        ai_reply, should_escalate = get_ai_reply(message)
        if should_escalate:
            is_complex = True
            faq_reply = None
        elif ai_reply is not None:
            return Response({"reply": ai_reply, "ticket_id": None}, status=status.HTTP_200_OK)
        else:
            faq_reply = _get_faq_reply(message)
            is_complex = _is_complex_query(message, faq_reply)
    else:
        faq_reply = _get_faq_reply(message)
        is_complex = _is_complex_query(message, faq_reply)

    if is_complex:
        # Create support ticket so the team is intimated
        subject = f"[Chat] {message[:80]}" + ("..." if len(message) > 80 else "")
        description = (
            "This ticket was auto-created from the website chat agent.\n\n"
            "User message:\n" + message
        )
        ticket_data = {
            "ticket_type": TicketTypeCode.OTHER,
            "subject": subject,
            "description": description,
            "priority": Ticket.TicketPriority.MEDIUM,
        }
        if request.user.is_authenticated:
            serializer = TicketCreateSerializer(data=ticket_data)
            if serializer.is_valid():
                ticket = serializer.save(user=request.user)
                return Response(
                    {
                        "reply": (
                            f"We've created support ticket #{ticket.ticket_id} for your query. "
                            "Our team will get back to you shortly. You can track it under Tickets."
                        ),
                        "ticket_id": ticket.ticket_id,
                    },
                    status=status.HTTP_200_OK,
                )
        else:
            ticket_data["public_name"] = public_name or "Chat user"
            ticket_data["public_email"] = public_email or "chat-escalation@internal"
            serializer = TicketCreateSerializer(data=ticket_data)
            if serializer.is_valid():
                ticket = serializer.save()
                return Response(
                    {
                        "reply": (
                            f"We've created support ticket #{ticket.ticket_id} for your query. "
                            "Our team will contact you at the email you provided (or the one on file)."
                        ),
                        "ticket_id": ticket.ticket_id,
                    },
                    status=status.HTTP_200_OK,
                )
        return Response(
            {"reply": "We're unable to create a ticket right now. Please try the Create Support Ticket option from the menu or contact us by email.", "ticket_id": None},
            status=status.HTTP_200_OK,
        )

    # FAQ fallback (when AI not used or AI failed)
    reply = faq_reply or (
        "I couldn't find a direct answer for that. If your query is complex or you need to speak to our team, "
        "say so and we will create a support ticket for you."
    )
    return Response({"reply": reply, "ticket_id": None}, status=status.HTTP_200_OK)
